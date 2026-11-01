import copy
from datetime import datetime, timedelta, timezone
import os
import tempfile
import unittest
from unittest.mock import Mock, patch

from sources import sulphur

os.environ.setdefault('LOUISIANA911_DB_PATH', os.path.join(tempfile.gettempdir(), f'louisiana911-tests-{os.getpid()}.db'))
import app


NOW = datetime(2026, 11, 1, 22, tzinfo=timezone.utc)
CALL = {
    'CallType': 'CLOSED', 'Agency': 'SPD', 'Service': 'LAW', 'IncidentId': '2610300725',
    'StartTime': '2026-10-30T23:45:43-05:00', 'EndTime': '2026-10-30T23:50:55-05:00',
    'Nature': 'L-SUSPICIOUS VEHICLE', 'Address': '400 BLK PICARD RD, SULPHUR',
    'HasLocation': True, 'Latitude': 30.2299, 'Longitude': -93.3855,
}


def response(payload):
    result = Mock()
    result.json.return_value = payload
    return result


class SulphurAdapterTests(unittest.TestCase):
    def normalize(self, **changes):
        return sulphur._normalize_row({**CALL, **changes}, now=NOW)

    def test_closed_police_calls_preserve_identity_public_label_and_time(self):
        row = self.normalize()
        self.assertEqual(row['source'], 'sulphur')
        self.assertEqual(row['source_id'], 'SPD:2610300725')
        self.assertEqual(row['description'], 'SUSPICIOUS VEHICLE')
        self.assertEqual(row['street'], CALL['Address'])
        self.assertEqual(row['municipality'], 'Sulphur')
        self.assertEqual(row['occurred_at'], '2026-10-31T04:45:43+00:00')
        self.assertEqual(row['time'], '2345')
        self.assertFalse(row['is_active'])
        self.assertEqual(row['units'], 0)
        self.assertTrue(row['location_is_approximate'])

    def test_law_fire_and_ems_prefixes_are_removed(self):
        for nature, label in (('L-TRAFFIC STOP', 'TRAFFIC STOP'), ('F-FIRE ALARM', 'FIRE ALARM'),
                              ('E-MEDICAL', 'MEDICAL'), ('WELFARE CONCERN', 'WELFARE CONCERN')):
            self.assertEqual(self.normalize(Nature=nature)['description'], label)

    def test_municipality_comes_from_the_published_address(self):
        self.assertEqual(self.normalize(Address='100 BLK SAMPSON ST, WESTLAKE')['municipality'], 'Westlake')
        self.assertEqual(self.normalize(Address='I-10 / RUTH ST')['municipality'], 'Sulphur')
        self.assertEqual(self.normalize(Address='1 BLK MAIN ST, ELSEWHERE')['municipality'], 'Sulphur')

    def test_only_closed_spd_calls_at_least_24_hours_old_are_imported(self):
        for changes in ({'CallType': 'OPEN'}, {'Agency': 'LCPD'}, {'Agency': 'CPSO'},
                        {'EndTime': '2026-11-01T17:00:01-05:00'}):
            self.assertIsNone(self.normalize(**changes))
        self.assertIsNotNone(self.normalize(EndTime='2026-10-31T17:00:00-05:00'))

    def test_withheld_invalid_and_out_of_area_points_remain_unmapped(self):
        for changes in ({'HasLocation': False}, {'Latitude': None}, {'Longitude': 0},
                        {'Latitude': float('nan')}, {'Longitude': float('inf')},
                        {'Latitude': 32.5, 'Longitude': -93.7}):
            row = self.normalize(**changes)
            self.assertIsNone(row['latitude'])
            self.assertIsNone(row['longitude'])
            self.assertFalse(row['coordinates_published'])

    def test_missing_or_ambiguous_timestamps_fail_instead_of_using_import_time(self):
        for changes in ({'StartTime': 'bad'}, {'StartTime': '2026-10-30T10:00:00'},
                        {'IncidentId': ''}, {'EndTime': None}):
            with self.assertRaises(ValueError):
                self.normalize(**changes)

    def session(self, pages, agency=None):
        session = Mock()
        session.__enter__ = Mock(return_value=session)
        session.__exit__ = Mock(return_value=False)
        session.headers = {}
        session.cookies.get.return_value = 'public-csrf-test-value'
        session.get.side_effect = [response(None), response(agency or {'Name': 'sulphur', 'AgencyId': 166}),
                                   response({'ClosedCallsEnabled': True})]
        session.post.side_effect = [response(page) if isinstance(page, dict) else page for page in pages]
        return session

    def test_normal_session_closed_only_pagination(self):
        older = {**CALL, 'IncidentId': '2610300724', 'Nature': 'L-CRASH'}
        session = self.session([{'CADCalls': [CALL, older], 'Total': 3},
                                {'CADCalls': [{**older, 'IncidentId': '2610300723'}], 'Total': 3}])
        with patch.object(sulphur.requests, 'Session', return_value=session), patch.object(sulphur, 'PAGE_SIZE', 2):
            rows, label = sulphur.scrape(user_agent='test', now=NOW)
        self.assertEqual(len(rows), 3)
        self.assertEqual(label, 'Oct 30, 23:45 CDT')
        self.assertEqual(session.headers['X-XSRF-TOKEN'], 'public-csrf-test-value')
        for index, call in enumerate(session.post.call_args_list):
            body = call.kwargs['json']
            self.assertFalse(body['IncludeOpenCalls'])
            self.assertTrue(body['IncludeClosedCalls'])
            self.assertEqual(body['PagingOptions']['Skip'], index * 2)

    def test_a_different_agency_on_the_host_fails_closed(self):
        session = self.session([], agency={'Name': 'lakecharles', 'AgencyId': 167})
        with patch.object(sulphur.requests, 'Session', return_value=session):
            with self.assertRaisesRegex(ValueError, 'configuration changed'):
                sulphur.scrape(user_agent='test', now=NOW)

    def test_short_missing_oversized_repeated_or_failed_pages_never_return_partial_results(self):
        for pages in ([{'CADCalls': [CALL], 'Total': 20}],
                      [{'CADCalls': [], 'Total': 1}],
                      [{'CADCalls': [CALL], 'Total': 2001}],
                      [{'CADCalls': [CALL], 'Total': '1'}],
                      [{'WrongSchema': []}],
                      [{'CADCalls': [CALL], 'Total': 2}, {'CADCalls': [CALL], 'Total': 2}],
                      [RuntimeError('upstream unavailable')]):
            with self.subTest(pages=pages), patch.object(sulphur.requests, 'Session', return_value=self.session(pages)), \
                    patch.object(sulphur, 'PAGE_SIZE', 1):
                with self.assertRaises((ValueError, RuntimeError)):
                    sulphur.scrape(user_agent='test', now=NOW)


class SulphurDatabaseTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        db_patch = patch.object(app, 'DB_PATH', os.path.join(self.directory.name, 'live.db'))
        db_patch.start()
        self.addCleanup(db_patch.stop)
        app.init_db()

    def test_closed_calls_keep_source_points_and_never_geocode(self):
        incident = sulphur._normalize_row(CALL, now=NOW)
        with patch.object(app, 'geocode_address', side_effect=AssertionError('must not geocode')):
            app.process_incidents([copy.copy(incident)], source='sulphur')
            app.process_incidents([{**incident, 'latitude': None, 'longitude': None}], source='sulphur')
        conn = app.db_connect(row_factory=True)
        self.addCleanup(conn.close)
        rows = conn.execute('SELECT * FROM incidents').fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['source'], 'sulphur')
        self.assertEqual(rows[0]['first_seen'], incident['occurred_at'])
        self.assertEqual(rows[0]['is_active'], 0)
        self.assertIsNone(rows[0]['latitude'])
        self.assertEqual(rows[0]['geocode_quality'], 'location-unavailable')
        self.assertEqual(app._incident_reported_at(dict(rows[0])), incident['occurred_at'])

    def test_latest_shows_bounded_slices_of_each_delayed_source(self):
        conn = app.db_connect()
        recent = datetime.now(timezone.utc) - timedelta(days=2)
        for source in ('sulphur', 'lakecharles'):
            for index in range(155):
                conn.execute('INSERT INTO incidents(hash, source, is_active, first_seen) VALUES (?,?,?,?)',
                             (f'{source}-{index}', source, 0, (recent + timedelta(minutes=index)).isoformat()))
        conn.execute('INSERT INTO incidents(hash, source, is_active, first_seen) VALUES (?,?,?,?)',
                     ('too-old', 'sulphur', 0, (recent - timedelta(days=10)).isoformat()))
        conn.commit()
        conn.close()
        with app.app.test_request_context('/api/incidents/active?source=sulphur'):
            rows = app.get_active_incidents().get_json()
        self.assertEqual(len(rows), 150)
        self.assertEqual(rows[0]['hash'], 'sulphur-154')
        self.assertTrue(all(row['source'] == 'sulphur' and row['is_active'] == 0 for row in rows))
        with app.app.test_request_context('/api/incidents/active?source=all'):
            self.assertEqual(len(app.get_active_incidents().get_json()), 300)
        with app.app.test_request_context('/api/incidents/active?source=calcasieu'):
            rows = app.get_active_incidents().get_json()
        self.assertEqual(len(rows), 300)
        self.assertEqual({row['source'] for row in rows}, {'sulphur', 'lakecharles'})

    def test_calcasieu_history_covers_both_feeds_only(self):
        conn = app.db_connect(row_factory=True)
        for name, source in (('lake', 'lakecharles'), ('sul', 'sulphur'), ('cad', 'caddo')):
            conn.execute('INSERT INTO incidents(hash, source, is_active, first_seen) VALUES (?,?,0,?)',
                         (name, source, '2026-10-30T15:00:00+00:00'))
        conn.commit()
        bounds = app._central_date_bounds_utc('2026-10-30')
        rows, total = app._query_history_candidates(conn, bounds, 'calcasieu', 10)
        self.assertEqual(total, 2)
        self.assertEqual({row['source'] for row in rows}, {'lakecharles', 'sulphur'})
        self.assertEqual(app._query_history_candidates(conn, bounds, 'sulphur', 10)[1], 1)
        conn.close()
        with app.app.test_request_context('/api/incidents/history_counts?month=2026-10&source=calcasieu'), \
                patch.object(app, '_history_ui_request_guard', return_value=None), \
                patch.object(app, '_get_archive_dbs_for_month', return_value=[]):
            counts = app.get_history_counts().get_json()['counts']
        self.assertEqual(counts.get('2026-10-30'), 2)

    def test_calcasieu_filter_and_refresh_label(self):
        self.assertEqual(app._normalize_source_filter('calcasieu'), 'calcasieu')
        self.assertEqual(app._normalize_source_filter('CALCASIEU'), 'calcasieu')
        self.assertEqual(app._normalize_source_filter('bogus'), 'all')
        self.assertEqual(app._latest_delayed_refresh_label(['Oct 30, 23:45 CDT', 'Oct 31, 00:04 CDT']), 'Oct 31, 00:04 CDT')
        self.assertEqual(app._latest_delayed_refresh_label([None, 'Oct 30, 23:45 CDT']), 'Oct 30, 23:45 CDT')
        self.assertIsNone(app._latest_delayed_refresh_label([None, None]))


if __name__ == '__main__':
    unittest.main()
