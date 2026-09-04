import copy
from datetime import datetime, timedelta, timezone
import os
import tempfile
import unittest
from unittest.mock import Mock, patch

from sources import lakecharles

os.environ.setdefault('LOUISIANA911_DB_PATH', os.path.join(tempfile.gettempdir(), f'louisiana911-tests-{os.getpid()}.db'))
import app


NOW = datetime(2026, 9, 4, 22, tzinfo=timezone.utc)
CALL = {
    'CallType': 'CLOSED', 'Agency': 'LCPD', 'Service': 'LAW', 'IncidentId': '2609030100',
    'StartTime': '2026-09-03T09:20:00-05:00', 'EndTime': '2026-09-03T10:05:00-05:00',
    'Nature': 'L-THEFT', 'Address': '100 BLK EXAMPLE ST, LAKE CHARLES',
    'HasLocation': True, 'Latitude': 30.22, 'Longitude': -93.21,
}


def response(payload):
    result = Mock()
    result.json.return_value = payload
    return result


class LakeCharlesAdapterTests(unittest.TestCase):
    def normalize(self, **changes):
        return lakecharles._normalize_row({**CALL, **changes}, now=NOW)

    def test_closed_police_calls_preserve_identity_public_label_and_time(self):
        row = self.normalize()
        self.assertEqual(row['source_id'], 'LCPD:2609030100')
        self.assertEqual(row['description'], 'THEFT')
        self.assertEqual(row['street'], CALL['Address'])
        self.assertEqual(row['occurred_at'], '2026-09-03T14:20:00+00:00')
        self.assertEqual(row['time'], '0920')
        self.assertFalse(row['is_active'])
        self.assertEqual(row['units'], 0)
        self.assertTrue(row['location_is_approximate'])

    def test_crashes_and_service_calls_are_not_filtered_out_as_non_crime(self):
        for nature in ('L-CRASH', 'L-WELFARE CONCERN', 'L-TRAFFIC STOP', 'L-SERVICE CALL POLICE'):
            self.assertIsNotNone(self.normalize(Nature=nature))

    def test_only_closed_lcpd_calls_at_least_24_hours_old_are_imported(self):
        for changes in ({'CallType': 'OPEN'}, {'Agency': 'SPD'}, {'Agency': 'VPD'},
                        {'EndTime': '2026-09-03T17:00:01-05:00'}):
            self.assertIsNone(self.normalize(**changes))
        self.assertIsNotNone(self.normalize(EndTime='2026-09-03T17:00:00-05:00'))

    def test_withheld_invalid_and_out_of_area_points_remain_unmapped(self):
        for changes in ({'HasLocation': False}, {'Latitude': None}, {'Longitude': 0},
                        {'Latitude': float('nan')}, {'Longitude': float('inf')},
                        {'Latitude': 32.5, 'Longitude': -93.7}):
            row = self.normalize(**changes)
            self.assertIsNone(row['latitude'])
            self.assertIsNone(row['longitude'])
            self.assertFalse(row['coordinates_published'])

    def test_missing_or_ambiguous_timestamps_fail_instead_of_using_import_time(self):
        for changes in ({'StartTime': 'bad'}, {'StartTime': '2026-09-03T10:00:00'},
                        {'IncidentId': ''}, {'EndTime': None}):
            with self.assertRaises(ValueError):
                self.normalize(**changes)

    def session(self, pages):
        session = Mock()
        session.__enter__ = Mock(return_value=session)
        session.__exit__ = Mock(return_value=False)
        session.headers = {}
        session.cookies.get.return_value = 'public-csrf-test-value'
        session.get.side_effect = [response(None), response({'Name': 'lakecharles', 'AgencyId': 167}),
                                   response({'ClosedCallsEnabled': True})]
        session.post.side_effect = [response(page) if isinstance(page, dict) else page for page in pages]
        return session

    def test_normal_session_closed_only_pagination_and_duplicate_identity(self):
        older = {**CALL, 'IncidentId': '2609030099', 'Nature': 'L-CRASH'}
        session = self.session([{'CADCalls': [CALL, older], 'Total': 3},
                                {'CADCalls': [{**older, 'Nature': 'L-CRASH UPDATED'}], 'Total': 3}])
        # A wholly repeated page is treated as pagination failure, not success.
        with patch.object(lakecharles.requests, 'Session', return_value=session), patch.object(lakecharles, 'PAGE_SIZE', 2):
            with self.assertRaisesRegex(ValueError, 'repeated a page'):
                lakecharles.scrape(user_agent='test', now=NOW)
        session = self.session([{'CADCalls': [CALL, older], 'Total': 3},
                                {'CADCalls': [{**older, 'IncidentId': '2609030098'}], 'Total': 3}])
        with patch.object(lakecharles.requests, 'Session', return_value=session), patch.object(lakecharles, 'PAGE_SIZE', 2):
            rows, _ = lakecharles.scrape(user_agent='test', now=NOW)
        self.assertEqual(len(rows), 3)
        self.assertEqual(session.headers['X-XSRF-TOKEN'], 'public-csrf-test-value')
        for index, call in enumerate(session.post.call_args_list):
            body = call.kwargs['json']
            self.assertFalse(body['IncludeOpenCalls'])
            self.assertTrue(body['IncludeClosedCalls'])
            self.assertEqual(body['PagingOptions']['Skip'], index * 2)
            self.assertEqual(body['FilterOptionsParameters']['Parameters'], [])

    def test_short_missing_oversized_or_failed_pages_never_return_partial_results(self):
        for pages in ([{'CADCalls': [CALL], 'Total': 20}],
                      [{'CADCalls': [], 'Total': 1}],
                      [{'CADCalls': [CALL], 'Total': 2001}],
                      [{'CADCalls': [CALL], 'Total': '1'}],
                      [{'WrongSchema': []}],
                      [RuntimeError('upstream unavailable')]):
            with self.subTest(pages=pages), patch.object(lakecharles.requests, 'Session', return_value=self.session(pages)):
                with self.assertRaises((ValueError, RuntimeError)):
                    lakecharles.scrape(user_agent='test', now=NOW)


class LakeCharlesDatabaseTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        db_patch = patch.object(app, 'DB_PATH', os.path.join(self.directory.name, 'live.db'))
        db_patch.start()
        self.addCleanup(db_patch.stop)
        app.init_db()

    def test_reimports_update_one_closed_record_and_clear_withheld_coordinates(self):
        incident = lakecharles._normalize_row(CALL, now=NOW)
        with patch.object(app, 'geocode_address', side_effect=AssertionError('must not geocode')):
            app.process_incidents([copy.copy(incident)], source='lakecharles')
            app.process_incidents([{**incident, 'description': 'THEFT UPDATED', 'latitude': None,
                                    'longitude': None}], source='lakecharles')
        conn = app.db_connect(row_factory=True)
        self.addCleanup(conn.close)
        rows = conn.execute('SELECT * FROM incidents').fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['description'], 'THEFT UPDATED')
        self.assertEqual(rows[0]['first_seen'], incident['occurred_at'])
        self.assertEqual(rows[0]['is_active'], 0)
        self.assertIsNone(rows[0]['latitude'])
        self.assertEqual(rows[0]['geocode_quality'], 'location-unavailable')
        candidates, total = app._query_history_candidates(conn, app._central_date_bounds_utc('2026-09-03'), 'lakecharles', 10)
        self.assertEqual(total, 1)

    def test_latest_slice_is_bounded_dated_and_does_not_change_active_counts(self):
        conn = app.db_connect()
        recent = datetime.now(timezone.utc) - timedelta(days=2)
        for index in range(160):
            conn.execute('INSERT INTO incidents(hash, source, is_active, first_seen) VALUES (?,?,?,?)',
                         (f'lake-{index}', 'lakecharles', 0, (recent + timedelta(minutes=index)).isoformat()))
        conn.execute('INSERT INTO incidents(hash, source, is_active, first_seen) VALUES (?,?,?,?)',
                     ('too-old', 'lakecharles', 0, (recent - timedelta(days=10)).isoformat()))
        conn.execute("INSERT INTO incidents(hash, source, is_active) VALUES ('caddo-active','caddo',1)")
        conn.commit()
        conn.close()
        with app.app.test_request_context('/api/incidents/active?source=lakecharles'):
            rows = app.get_active_incidents().get_json()
        self.assertEqual(len(rows), 150)
        self.assertEqual(rows[0]['hash'], 'lake-159')
        self.assertTrue(all(row['source'] == 'lakecharles' and row['is_active'] == 0 for row in rows))
        with app.app.test_request_context('/api/incidents/active?source=all'):
            self.assertEqual(len(app.get_active_incidents().get_json()), 151)
        with app.app.test_request_context('/api/incidents/active?source=caddo'):
            self.assertEqual(len(app.get_active_incidents().get_json()), 1)


if __name__ == '__main__':
    unittest.main()
