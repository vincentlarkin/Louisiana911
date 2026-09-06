import os
import tempfile
import unittest
from contextlib import ExitStack
from unittest.mock import Mock, patch

os.environ.setdefault('LOUISIANA911_DB_PATH', os.path.join(tempfile.gettempdir(), f'live-feed-tests-{os.getpid()}.db'))
import app
from sources import batonrouge, lafayette


BR_HEADERS = '<tr>' + ''.join(f'<th>{v}</th>' for v in ['Time', 'Type', 'Agency', 'Location', 'Cross Street']) + '</tr>'
BR_ROW = '<tr><td>11:50:20 PM</td><td>CRASH</td><td>LAW</td><td>MAIN</td><td>OAK</td></tr>'
LF_HEADERS = '<tr>' + ''.join(f'<td><b>{v}</b></td>' for v in ['Located At', 'Due To', 'Reported At', 'Assisting']) + '</tr>'
LF_ROW = '<tr><td>MAIN/OAK LAFAYETTE, LA</td><td>CRASH</td><td>09/04/2026 23:50</td><td>POLICE</td></tr>'


class LiveAdapterTests(unittest.TestCase):
    def baton(self, rows=BR_ROW, count=1, transform=lambda s: s):
        html = f'<p>Last Updated 9/5/2026 12:10:00 AM</p><p>Number of incidents: {count}</p><table class="TabTraf">{BR_HEADERS}{rows}</table>'
        with patch.object(batonrouge.requests, 'get', return_value=Mock(text=transform(html))), \
             patch.object(batonrouge, '_fetch_published_features', return_value=[]):
            return batonrouge.scrape(user_agent='test')

    def lafayette(self, rows=LF_ROW, **kwargs):
        payload = dict(success=True, data=f'<table>{LF_HEADERS}{rows}</table>')
        payload.update(kwargs)
        with patch.object(lafayette.requests, 'get', return_value=Mock(json=lambda: payload)):
            return lafayette.scrape(user_agent='test')

    def test_baton_rouge_valid_and_empty_snapshots(self):
        calls, refresh = self.baton()
        self.assertEqual(calls[0]['time'], '2350')
        self.assertTrue(refresh)
        self.assertEqual(self.baton(rows='', count=0), ([], refresh))

    def test_baton_rouge_failure_or_partial_page_is_not_an_empty_snapshot(self):
        for kwargs in [dict(rows='', count=1), dict(count=0),
                       dict(transform=lambda s: '<html>Unavailable</html>'),
                       dict(transform=lambda s: s.replace('Cross Street', 'Changed')),
                       dict(rows=BR_ROW.replace('11:50:20 PM', 'bad time')),
                       dict(rows=BR_ROW.replace('<td>OAK</td>', ''))]:
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                self.baton(**kwargs)

    def test_lafayette_preserves_published_date_instead_of_observation_date(self):
        calls, refresh = self.lafayette()
        self.assertIsNone(refresh)  # Validity does not depend on a refresh label.
        self.assertEqual(calls[0]['time'], '2350')
        self.assertEqual(calls[0]['occurred_at'], '2026-09-05T04:50:00+00:00')
        self.assertEqual(self.lafayette(rows=''), ([], None))

    def test_lafayette_failed_or_malformed_snapshots_raise(self):
        for kwargs in [dict(success=False), dict(data=''), dict(data='<h1>Unavailable</h1>'),
                       dict(rows=LF_ROW.replace('09/04/2026 23:50', '23:50')),
                       dict(rows=LF_ROW.replace('<td>POLICE</td>', '')),
                       dict(rows=LF_ROW + LF_ROW.replace('<td>CRASH</td>', '<td></td>'))]:
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                self.lafayette(**kwargs)

    def test_failed_live_requests_are_distinct_from_successful_empty_results(self):
        for source in app.LIVE_SNAPSHOT_SOURCES:
            with self.subTest(source=source), patch.object(getattr(app, source + '_source'), 'scrape', side_effect=ValueError('bad snapshot')), \
                 patch.object(app, 'log'), patch('traceback.print_exc'):
                self.assertEqual(getattr(app, 'scrape_' + source + '_incidents')(), (None, None))


class LiveLifecycleTests(unittest.TestCase):
    def test_each_live_source_removes_missing_calls_but_preserves_history_and_other_sources(self):
        for source in app.LIVE_SNAPSHOT_SOURCES:
            with self.subTest(source=source), tempfile.TemporaryDirectory() as directory, \
                 patch.object(app, 'DB_PATH', os.path.join(directory, 'live.db')):
                app.init_db()
                conn = app.db_connect(row_factory=True)
                try:
                    for other in app.VALID_SOURCES:
                        conn.execute('INSERT INTO incidents(hash,source,is_active,first_seen) VALUES (?,?,1,?)',
                                     (other, other, '2026-09-05T12:00:00+00:00'))
                    conn.commit()
                    app.process_incidents(None, source=source, allow_empty=True)
                    self.assertEqual(conn.execute('SELECT SUM(is_active) FROM incidents').fetchone()[0], 5)
                    app.process_incidents([], source=source, allow_empty=True)
                    self.assertEqual(conn.execute('SELECT SUM(is_active) FROM incidents').fetchone()[0], 4)
                    rows, count = app._query_history_candidates(conn, app._central_date_bounds_utc('2026-09-05'), source, 10)
                    self.assertEqual(count, 1)
                    self.assertEqual(rows[0]['source'], source)
                    self.assertEqual(conn.execute('SELECT COUNT(*) FROM incidents').fetchone()[0], 5)
                finally:
                    conn.close()

    def test_collector_accepts_empty_live_feeds_without_clearing_delayed_feeds(self):
        with ExitStack() as stack:
            stack.enter_context(patch.object(app, 'meta_set'))
            stack.enter_context(patch.object(app, '_store_feed_refresh'))
            stack.enter_context(patch.dict(app.source_last_scrape_monotonic, {}, clear=True))
            for source in app.VALID_SOURCES:
                stack.enter_context(patch.object(app, f'scrape_{source}_incidents', return_value=([], None)))
            process = stack.enter_context(patch.object(app, 'process_incidents'))
            app.background_scrape()
            self.assertEqual({call.kwargs['source'] for call in process.call_args_list}, app.LIVE_SNAPSHOT_SOURCES)
            self.assertTrue(all(call.kwargs['allow_empty'] for call in process.call_args_list))

    def test_manual_refresh_tolerates_failures_and_accepts_valid_empty_snapshots(self):
        with patch.dict(os.environ, {'LOUISIANA911_ENABLE_REFRESH_ENDPOINT': '1'}), \
             patch.object(app, 'meta_set'), patch.object(app, '_store_feed_refresh'), \
             patch.object(app, 'scrape_caddo_incidents', return_value=(None, None)), \
             patch.object(app, 'scrape_batonrouge_incidents', return_value=([], 'refresh')), \
             patch.object(app, 'scrape_lafayette_incidents', return_value=([], None)), \
             patch.object(app, 'process_incidents') as process, \
             app.app.test_request_context('/api/refresh', method='POST'):
            self.assertTrue(app.force_refresh().get_json()['success'])
            self.assertEqual([call.kwargs['allow_empty'] for call in process.call_args_list], [False, True, True])

    def test_baton_rouge_overnight_date_and_delayed_original_dates(self):
        self.assertEqual(app._incident_reported_at(dict(source='batonrouge', time='2350',
                         first_seen='2026-09-05T05:10:00Z')), '2026-09-05T04:50:00+00:00')
        for source in ['neworleans', 'lakecharles', 'lafayette']:
            call = dict(source=source, time='2350', first_seen='2026-09-02T04:50:00+00:00', last_seen='2026-09-05T12:00:00Z')
            self.assertEqual(app._incident_reported_at(call), '2026-09-02T04:50:00+00:00')

    def test_lafayette_published_date_survives_ingestion_and_repeated_polls(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(app, 'DB_PATH', os.path.join(directory, 'live.db')), \
             patch.object(app, '_incident_geocode_result', return_value={'lat': 30.2, 'lng': -92.0}):
            app.init_db()
            call = dict(source='lafayette', agency='POLICE', time='2350', units=1, description='CRASH',
                        street='MAIN', cross_streets='OAK', municipality='LAFAYETTE',
                        occurred_at='2026-09-02T04:50:00+00:00')
            app.process_incidents([dict(call)], source='lafayette')
            app.process_incidents([dict(call)], source='lafayette')
            app.process_incidents([], source='lafayette', allow_empty=True)
            conn = app.db_connect(row_factory=True)
            try:
                row = dict(conn.execute('SELECT * FROM incidents').fetchone())
                self.assertEqual(app._public_incident(row)['reported_at'], call['occurred_at'])
                self.assertEqual(row['is_active'], 0)
                self.assertEqual(app._query_history_candidates(conn, app._central_date_bounds_utc('2026-09-01'), 'lafayette', 10)[1], 1)
            finally:
                conn.close()
