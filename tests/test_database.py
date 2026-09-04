import os
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

os.environ.setdefault('LOUISIANA911_DB_PATH', os.path.join(tempfile.gettempdir(), f'louisiana911-db-tests-{os.getpid()}.db'))
import app


class DatabaseTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.db_patch = patch.object(app, 'DB_PATH', os.path.join(self.directory.name, 'live.db'))
        self.db_patch.start()
        self.addCleanup(self.db_patch.stop)
        app.init_db()

    def insert(self, conn, key, timestamp, source='caddo', active=0):
        conn.execute('INSERT INTO incidents(hash, first_seen, source, is_active) VALUES (?,?,?,?)',
                     (key, timestamp, source, active))

    def history(self, query):
        with app.app.test_request_context('/api/incidents/history?' + query), patch.object(app, '_history_ui_request_guard', return_value=None):
            result = app.get_history()
            if isinstance(result, tuple):
                return result[0].get_json(), result[1]
            return result.get_json(), 200

    def test_history_merges_bounded_pages_across_databases_with_stable_ties(self):
        archive_path = app._get_archive_db_path(2026, 7)
        app._init_archive_db(archive_path)
        live = app.db_connect()
        archive = app._archive_db_connect(archive_path)
        for number in range(60):
            target = live if number % 2 else archive
            self.insert(target, f'call-{number}', f'2026-07-20T12:{number // 2:02d}:00+00:00')
        self.insert(live, 'active', '2026-07-20T15:00:00+00:00', active=1)
        self.insert(live, 'outside', '2026-07-21T05:00:00+00:00')
        live.commit(); archive.commit(); live.close(); archive.close()
        seen = []
        original = app._query_history_candidates
        def track(*args, **kwargs):
            rows, total = original(*args, **kwargs)
            seen.append((args[3], len(rows)))
            return rows, total
        with patch.object(app, '_query_history_candidates', side_effect=track):
            first, status = self.history('date=2026-07-20&limit=7')
            second, _ = self.history('date=2026-07-20&limit=7&offset=7')
            full, _ = self.history('date=2026-07-20&limit=100')
        self.assertEqual(status, 200)
        self.assertEqual(first['total'], 60)
        self.assertEqual(first['incidents'] + second['incidents'], full['incidents'][:14])
        self.assertEqual(len({row['hash'] for row in first['incidents'] + second['incidents']}), 14)
        self.assertTrue(all(count <= cap for cap, count in seen))
        self.assertEqual(seen[:2], [(7, 7), (7, 7)])

    def test_history_retains_legacy_source_support_without_writing_schema(self):
        archive = sqlite3.connect(':memory:')
        self.addCleanup(archive.close)
        archive.row_factory = sqlite3.Row
        archive.execute('CREATE TABLE incidents(id INTEGER PRIMARY KEY, hash TEXT, first_seen TEXT)')
        archive.execute("INSERT INTO incidents VALUES (1,'legacy','2026-07-20T12:00:00+00:00')")
        archive.commit()
        bounds = app._central_date_bounds_utc('2026-07-20')
        rows, total = app._query_history_candidates(archive, bounds, 'caddo', 10, archived=True)
        self.assertEqual((len(rows), total), (1, 1))
        self.assertEqual(app._query_history_candidates(archive, bounds, 'neworleans', 10, archived=True), ([], 0))
        self.assertNotIn('source', [r['name'] for r in archive.execute('PRAGMA table_info(incidents)')])

    def test_history_filters_source_and_uses_central_dst_day_bounds(self):
        conn = app.db_connect(row_factory=True)
        self.addCleanup(conn.close)
        for key, timestamp, source in [
            ('before', '2026-11-01T04:59:59+00:00', 'caddo'),
            ('start', '2026-11-01T05:00:00+00:00', None),
            ('last', '2026-11-02T05:59:59+00:00', 'caddo'),
            ('end', '2026-11-02T06:00:00+00:00', 'caddo'),
            ('other', '2026-11-01T12:00:00+00:00', 'neworleans'),
        ]:
            self.insert(conn, key, timestamp, source)
        conn.commit()
        rows, total = app._query_history_candidates(conn, app._central_date_bounds_utc('2026-11-01'), 'caddo', 10)
        self.assertEqual(total, 2)
        self.assertEqual([r['hash'] for r in rows], ['last', 'start'])

    def test_bad_date_is_rejected_before_opening_database(self):
        with patch.object(app, 'db_connect') as connect:
            _, status = self.history('date=2026-02-30')
        self.assertEqual(status, 400)
        connect.assert_not_called()

    def test_extreme_offset_returns_empty_page_without_integer_overflow(self):
        body, status = self.history('date=2026-07-20&offset=' + '9' * 40)
        self.assertEqual(status, 200)
        self.assertEqual(body, {'incidents': [], 'total': 0})

    def test_archive_failure_returns_retryable_error_instead_of_incomplete_history(self):
        with patch.object(app, '_get_archive_dbs_for_date', return_value=['broken.db']), \
                patch.object(app, '_archive_db_connect', side_effect=sqlite3.OperationalError('unavailable')):
            body, status = self.history('date=2026-07-20')
        self.assertEqual(status, 503)
        self.assertIn('error', body)
        self.assertNotIn('incidents', body)

    def test_history_range_uses_ordered_index(self):
        conn = app.db_connect()
        self.addCleanup(conn.close)
        plan = ' '.join(row[3] for row in conn.execute(
            'EXPLAIN QUERY PLAN SELECT * FROM incidents WHERE is_active=0 AND first_seen >= ? AND first_seen < ? ORDER BY first_seen DESC, id DESC LIMIT 100',
            ('2026-07-20', '2026-07-21')))
        self.assertIn('idx_active_first_seen', plan)
        self.assertNotIn('TEMP B-TREE', plan)

    def test_geocoding_does_not_hold_writer_lock_for_new_or_existing_incidents(self):
        calls = []
        def geocode(incident, source):
            # A separate writer must be able to proceed during a slow provider call.
            conn = sqlite3.connect(app.DB_PATH, timeout=0)
            try:
                conn.execute('BEGIN IMMEDIATE')
                conn.rollback()
            finally:
                conn.close()
            calls.append(incident['street'])
            return {'lat': 32.5, 'lng': -93.7, 'source': 'arcgis', 'quality': 'street+cross', 'query': 'test'}
        incidents = [dict(source='caddo', agency='SFD', time='1200', units=5, description='FIRE',
                          street=f'TEST {number}', cross_streets='MAIN ST', municipality='SHV') for number in range(2)]
        with patch.object(app, '_incident_geocode_result', side_effect=geocode):
            app.process_incidents(incidents)
            with patch.object(app, 'GEOCODER_VERSION', app.GEOCODER_VERSION + 1):
                app.process_incidents(incidents)
        self.assertEqual(len(calls), 4)

    def test_archive_refreshes_existing_copy_and_reuses_database_pages(self):
        conn = app.db_connect()
        self.insert(conn, 'archived-call', '2020-01-01T12:00:00+00:00')
        conn.execute("UPDATE incidents SET description='original' WHERE hash='archived-call'")
        conn.commit(); conn.close()
        queries = []
        connect = app.db_connect
        def traced(**kwargs):
            connection = connect(**kwargs)
            connection.set_trace_callback(queries.append)
            return connection
        with patch.object(app, 'db_connect', side_effect=traced):
            result = app.archive_old_incidents()
        self.assertEqual(result['archived'], 1)
        self.assertFalse(any('VACUUM' in sql.upper() for sql in queries))
        conn = app.db_connect()
        self.assertEqual(conn.execute('SELECT COUNT(*) FROM incidents').fetchone()[0], 0)
        self.insert(conn, 'archived-call', '2020-01-01T12:00:00+00:00')
        conn.execute("UPDATE incidents SET description='corrected' WHERE hash='archived-call'")
        conn.commit(); conn.close()
        app.archive_old_incidents()
        conn = app._archive_db_connect(result['files'][0])
        self.addCleanup(conn.close)
        self.assertEqual(conn.execute('SELECT description FROM incidents').fetchall(), [('corrected',)])

    def test_archive_does_not_delete_incident_changed_since_snapshot(self):
        conn = app.db_connect()
        self.insert(conn, 'reactivated', '2020-01-01T12:00:00+00:00')
        conn.commit(); conn.close()
        def concurrent_update(message):
            if message.startswith('[ARCHIVE] Removing'):
                conn = app.db_connect()
                conn.execute("UPDATE incidents SET is_active=1,last_seen='2026-09-04T12:00:00+00:00' WHERE hash='reactivated'")
                conn.commit(); conn.close()
        with patch.object(app, 'log', side_effect=concurrent_update):
            app.archive_old_incidents()
        conn = app.db_connect()
        self.addCleanup(conn.close)
        self.assertEqual(conn.execute("SELECT is_active FROM incidents WHERE hash='reactivated'").fetchone(), (1,))

    def test_published_coordinates_keep_batched_writes(self):
        queries = []
        connect = app.db_connect
        def traced(**kwargs):
            connection = connect(**kwargs)
            connection.set_trace_callback(queries.append)
            return connection
        incidents = [dict(source='caddo', agency='SFD', time='1200', units=5, description='FIRE',
                          street=f'TEST {number}', cross_streets='MAIN ST', municipality='SHV',
                          latitude=32.5, longitude=-93.75) for number in range(3)]
        with patch.object(app, 'db_connect', side_effect=traced), patch.object(app, 'log'):
            app.process_incidents(incidents)
        # One incident batch plus one metadata update, not a commit for each row.
        self.assertEqual(sum(sql == 'COMMIT' for sql in queries), 2)

class AccessProtectionTests(unittest.TestCase):
    def setUp(self):
        with app._report_rate_lock:
            app._report_rate_hits.clear()
            app._history_date_rate_hits.clear()

    def request_limit(self, date, at, **headers):
        with app.app.test_request_context('/api/incidents/history?date=' + date, headers=headers), patch.object(app.time, 'monotonic', return_value=at):
            return app._check_report_rate_limit()

    def test_forwarded_ip_is_ignored_without_explicit_proxy_trust(self):
        with app.app.test_request_context('/', headers={'X-Forwarded-For': '203.0.113.9'}), patch.object(app, 'TRUSTED_PROXY_HOPS', 0):
            self.assertEqual(app._client_ip_for_rate_limit(), 'unknown')

    def test_trusted_proxy_reads_from_right_and_rejects_invalid_ip(self):
        with patch.object(app, 'TRUSTED_PROXY_HOPS', 1):
            with app.app.test_request_context('/', headers={'X-Forwarded-For': 'forged, 203.0.113.9'}):
                self.assertEqual(app._client_ip_for_rate_limit(), '203.0.113.9')
            with app.app.test_request_context('/', headers={'X-Forwarded-For': 'invalid'}):
                self.assertEqual(app._client_ip_for_rate_limit(), 'unknown')

    def test_hourly_quota_survives_minute_windows_and_allows_same_date_pages(self):
        with patch.object(app, 'HISTORY_HOURLY_DATE_LIMIT', 2), patch.object(app, 'HISTORY_DAILY_DATE_LIMIT', 10):
            self.assertIsNone(self.request_limit('2026-07-01', 1000))
            self.assertIsNone(self.request_limit('2026-07-02', 1100))
            self.assertIsNone(self.request_limit('2026-07-02', 1200))
            blocked = self.request_limit('2026-07-03', 1300)
            self.assertEqual(blocked.status_code, 429)
            self.assertGreater(int(blocked.headers['Retry-After']), 60)
            self.assertIsNone(self.request_limit('2026-07-03', 5000))

    def test_daily_quota_survives_hourly_windows(self):
        with patch.object(app, 'HISTORY_HOURLY_DATE_LIMIT', 10), patch.object(app, 'HISTORY_DAILY_DATE_LIMIT', 2):
            self.assertIsNone(self.request_limit('2026-07-01', 1000))
            self.assertIsNone(self.request_limit('2026-07-02', 5000))
            self.assertEqual(self.request_limit('2026-07-03', 10000).status_code, 429)
            self.assertIsNone(self.request_limit('2026-07-03', 90000))

    def test_repeat_date_requests_cannot_bypass_request_budget(self):
        with patch.object(app, 'INCIDENT_HISTORY_REQUEST_RATE_LIMIT', 2):
            self.assertIsNone(self.request_limit('2026-07-01', 1000))
            self.assertIsNone(self.request_limit('2026-07-01', 1001))
            self.assertEqual(self.request_limit('2026-07-01', 1002).status_code, 429)

    def test_calendar_date_spellings_share_quota_entry(self):
        with patch.object(app, 'INCIDENT_HISTORY_API_RATE_LIMIT', 1):
            self.assertIsNone(self.request_limit('2026-7-1', 1000))
            self.assertIsNone(self.request_limit('2026-07-01', 1001))
            self.assertEqual(self.request_limit('2026-07-02', 1002).status_code, 429)

    def test_public_payload_does_not_expose_internal_database_columns(self):
        payload = app._public_incident({'id': 1, 'hash': 'public-share-id', 'description': 'FIRE',
                                       'latitude': 32.5, 'longitude': -93.75, 'geocode_quality': 'published',
                                       'geocode_query': 'internal query', 'geocoded_at': 'internal timestamp',
                                       'geocode_version': 7, 'future_internal_column': 'private'})
        self.assertEqual(payload['description'], 'FIRE')
        self.assertEqual(payload['hash'], 'public-share-id')
        for key in ('geocode_query', 'geocoded_at', 'geocode_version', 'future_internal_column'):
            self.assertNotIn(key, payload)

    def test_persistent_date_quota_survives_reopen_and_stores_no_raw_ip(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(app, 'ACCESS_LIMIT_DB', os.path.join(directory, 'limits.db')), \
                patch.object(app, 'HISTORY_HOURLY_DATE_LIMIT', 1), patch.object(app, 'HISTORY_DAILY_DATE_LIMIT', 2), \
                patch.object(app.time, 'time', return_value=100000), app.app.test_request_context('/'):
            self.assertIsNone(app._persistent_history_date_limit('203.0.113.9', '2026-07-01'))
            app._history_date_rate_hits.clear()
            self.assertEqual(app._persistent_history_date_limit('203.0.113.9', '2026-07-02').status_code, 429)
            self.assertIsNone(app._persistent_history_date_limit('203.0.113.9', '2026-07-01'))
            self.assertIsNone(app._persistent_history_date_limit('203.0.113.10', '2026-07-02'))
            conn = sqlite3.connect(app.ACCESS_LIMIT_DB)
            try:
                keys = [row[0] for row in conn.execute('SELECT client_key FROM history_access')]
                self.assertTrue(all(len(key) == 64 and '203.0.113' not in key for key in keys))
            finally:
                conn.close()

    def test_unavailable_quota_store_does_not_disable_limits(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(app, 'ACCESS_LIMIT_DB', os.path.join(directory, 'missing', 'limits.db')), \
                app.app.test_request_context('/'):
            response = app._persistent_history_date_limit('203.0.113.9', '2026-07-01')
            self.assertEqual(response.status_code, 429)
            self.assertEqual(response.headers['Retry-After'], '60')
