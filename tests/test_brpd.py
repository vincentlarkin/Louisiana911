import copy
from datetime import datetime, timedelta, timezone
import os
import tempfile
import unittest
from unittest.mock import Mock, patch

from sources import brpd

os.environ.setdefault('LOUISIANA911_DB_PATH', os.path.join(tempfile.gettempdir(), f'louisiana911-tests-{os.getpid()}.db'))
import app


NOW = datetime(2026, 11, 11, 18, tzinfo=timezone.utc)


def wall_ms(year, month, day, hour, minute, second=0):
    """The layer's convention: Central wall-clock time stored as if it were UTC."""
    return int(datetime(year, month, day, hour, minute, second, tzinfo=timezone.utc).timestamp() * 1000)


CALL = {
    'attributes': {
        'IncidentNumber': 'P261109-0025', 'FinalProblemType': 'Alarm Duress Panic or Business',
        'Address': '4637 FLORIDA BLVD', 'City': 'B.R.',
        'CallDate': wall_ms(2026, 11, 9, 2, 17, 18), 'ClosedDate': wall_ms(2026, 11, 9, 2, 30, 36),
    },
    'geometry': {'x': -91.142856, 'y': 30.451119},
}


def call(geometry=None, **attributes):
    feature = copy.deepcopy(CALL)
    feature['attributes'].update(attributes)
    if geometry is not None:
        feature['geometry'] = geometry
    return feature


def response(payload):
    result = Mock()
    result.json.return_value = payload
    return result


class BatonRougePoliceAdapterTests(unittest.TestCase):
    def normalize(self, geometry=None, **attributes):
        return brpd._normalize_feature(call(geometry, **attributes), now=NOW)

    def test_calls_keep_identity_type_and_central_time(self):
        row = self.normalize()
        self.assertEqual(row['source'], 'brpd')
        self.assertEqual(row['source_id'], 'BRPD:P261109-0025')
        self.assertEqual(row['agency'], 'BRPD')
        self.assertEqual(row['description'], 'Alarm Duress Panic or Business')
        self.assertEqual(row['municipality'], 'Baton Rouge')
        # 2:17 AM Central Standard Time, though the layer labels the field UTC.
        self.assertEqual(row['time'], '0217')
        self.assertEqual(row['occurred_at'], '2026-11-09T08:17:18+00:00')
        self.assertFalse(row['is_active'])
        self.assertEqual(row['units'], 0)

    def test_summer_calls_use_daylight_time(self):
        row = self.normalize(CallDate=wall_ms(2026, 7, 4, 21, 5), ClosedDate=wall_ms(2026, 7, 4, 21, 40))
        self.assertEqual(row['time'], '2105')
        self.assertEqual(row['occurred_at'], '2026-07-05T02:05:00+00:00')

    def test_addresses_show_the_block_and_points_are_moved_to_a_coarse_grid(self):
        row = self.normalize()
        self.assertEqual(row['street'], '4600 BLK FLORIDA BLVD')
        self.assertEqual((row['latitude'], row['longitude']), (30.451, -91.143))
        self.assertTrue(row['location_is_approximate'])
        self.assertTrue(row['coordinates_published'])
        for address, block in (('5 S INTERSTATE 110 HWY', '0 BLK S INTERSTATE 110 HWY'),
                               ('5151 - 12 PLANK RD', '5100 BLK PLANK RD'),
                               ("15500 S HARRELL'S FERRY RD", "15500 BLK S HARRELL'S FERRY RD"),
                               ('RIVER RD', 'RIVER RD')):
            self.assertEqual(self.normalize(Address=address)['street'], block)

    def test_exact_locations_can_be_switched_back_on(self):
        with patch.object(brpd, 'GENERALIZE_LOCATIONS', False):
            row = self.normalize()
        self.assertEqual(row['street'], '4637 FLORIDA BLVD')
        self.assertEqual((row['latitude'], row['longitude']), (30.451119, -91.142856))
        self.assertFalse(row['location_is_approximate'])

    def test_corners_split_into_street_and_cross_street(self):
        for address, parts in (('AIRLINE HWY / TOM DR', ('AIRLINE HWY', 'TOM DR')),
                               ('79TH AVE / SCENIC HWY', ('79TH AVE', 'SCENIC HWY')),
                               ('3RD ST / FLORIDA ST', ('3RD ST', 'FLORIDA ST'))):
            row = self.normalize(Address=address)
            self.assertEqual((row['street'], row['cross_streets']), parts)

    def test_cad_prefixes_are_removed_from_call_types(self):
        for problem, label in (('..LAW 911 HANG UP', '911 HANG UP'),
                               ('..LAW 913 (SOUNDS OF WEAPONS)', '913 (SOUNDS OF WEAPONS)'),
                               ('..HP LAW * HIGH PRIORITY *', 'HIGH PRIORITY'),
                               ('Shots Fired-Shotspotter', 'Shots Fired-Shotspotter')):
            self.assertEqual(self.normalize(FinalProblemType=problem)['description'], label)

    def test_traffic_calls_already_on_the_live_feed_and_placeholders_are_skipped(self):
        for problem in ('Traffic Crash Off Roadway', 'Traffic Crash with Injuries', 'Hit & Run In Roadway',
                        'Hit and Run In Parking Lot', 'Abandoned Stalled Veh In Rdway', 'Traffic Congestion Blockage',
                        '.Law Default', 'C2C Default', '', None):
            self.assertIsNone(self.normalize(FinalProblemType=problem), problem)
        for problem in ('Traffic Violation', 'Street Racing/Veh. Rally', 'Vehicle Burglary'):
            self.assertIsNotNone(self.normalize(FinalProblemType=problem), problem)

    def test_calls_less_than_24_hours_old_are_never_imported(self):
        # NOW is noon Central on Nov 11.
        self.assertIsNone(self.normalize(CallDate=wall_ms(2026, 11, 10, 12, 0, 1), ClosedDate=None))
        self.assertIsNone(self.normalize(CallDate=wall_ms(2026, 11, 10, 9, 0), ClosedDate=wall_ms(2026, 11, 10, 13, 0)))
        self.assertIsNotNone(self.normalize(CallDate=wall_ms(2026, 11, 10, 11, 0), ClosedDate=None))

    def test_missing_invalid_and_out_of_parish_points_remain_unmapped(self):
        for geometry in ({}, {'x': None, 'y': 30.45}, {'x': 0, 'y': 0}, {'x': float('nan'), 'y': 30.45},
                         {'x': -93.75, 'y': 32.52}, {'x': 3320000.0, 'y': 700000.0}):
            row = self.normalize(geometry=geometry)
            self.assertIsNone(row['latitude'])
            self.assertIsNone(row['longitude'])
            self.assertFalse(row['coordinates_published'])

    def test_st_george_calls_are_labeled(self):
        self.assertEqual(self.normalize(City='ST.GEORGE')['municipality'], 'St. George')
        self.assertEqual(self.normalize(City='LSU')['municipality'], 'Baton Rouge')

    def test_missing_fields_or_call_times_fail_instead_of_using_import_time(self):
        for changes in ({'CallDate': None}, {'CallDate': 'yesterday'}, {'IncidentNumber': ''}):
            with self.assertRaises(ValueError):
                self.normalize(**changes)
        broken = call()
        del broken['attributes']['Address']
        with self.assertRaises(ValueError):
            brpd._normalize_feature(broken, now=NOW)

    def session(self, pages):
        session = Mock()
        session.__enter__ = Mock(return_value=session)
        session.__exit__ = Mock(return_value=False)
        session.headers = {}
        session.get.side_effect = [response(page) if isinstance(page, dict) else page for page in pages]
        return session

    def test_pages_until_the_layer_reports_no_more_rows(self):
        older = call(IncidentNumber='P261109-0023', FinalProblemType='Traffic Crash In Roadway')
        oldest = call(IncidentNumber='P261109-0021')
        session = self.session([{'features': [CALL, older], 'exceededTransferLimit': True}, {'features': [oldest]}])
        with patch.object(brpd.requests, 'Session', return_value=session), patch.object(brpd, 'PAGE_SIZE', 2):
            rows, label = brpd.scrape(user_agent='test', now=NOW)
        self.assertEqual([row['source_id'] for row in rows], ['BRPD:P261109-0025', 'BRPD:P261109-0021'])
        self.assertEqual(label, 'Nov 09, 02:17 CST')
        params = [request.kwargs['params'] for request in session.get.call_args_list]
        self.assertEqual([page['resultOffset'] for page in params], [0, 2])
        # The window is given in Central wall-clock time, matching the layer.
        self.assertEqual(params[0]['where'], "CallDate >= TIMESTAMP '2026-11-04 12:00:00'")

    def test_error_malformed_oversized_repeated_or_failed_pages_never_return_partial_results(self):
        for pages in ([{'error': {'code': 400}}],
                      [{'WrongSchema': []}],
                      [{'features': [CALL, CALL]}],
                      [{'features': [], 'exceededTransferLimit': True}],
                      [{'features': [CALL], 'exceededTransferLimit': True}, {'features': [CALL]}],
                      [{'features': [{'attributes': {'IncidentNumber': 'P1'}}]}],
                      [RuntimeError('upstream unavailable')]):
            with self.subTest(pages=pages), patch.object(brpd.requests, 'Session', return_value=self.session(pages)), \
                    patch.object(brpd, 'PAGE_SIZE', 1):
                with self.assertRaises((ValueError, RuntimeError)):
                    brpd.scrape(user_agent='test', now=NOW)

    def test_a_layer_that_never_stops_paging_fails(self):
        pages = [{'features': [call(IncidentNumber=f'P{index}')], 'exceededTransferLimit': True} for index in range(3)]
        with patch.object(brpd.requests, 'Session', return_value=self.session(pages)), \
                patch.object(brpd, 'PAGE_SIZE', 1), patch.object(brpd, 'MAX_PAGES', 3):
            with self.assertRaisesRegex(ValueError, 'pagination limit'):
                brpd.scrape(user_agent='test', now=NOW)


class BatonRougePoliceDatabaseTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        db_patch = patch.object(app, 'DB_PATH', os.path.join(self.directory.name, 'live.db'))
        db_patch.start()
        self.addCleanup(db_patch.stop)
        app.init_db()

    def test_calls_are_stored_closed_under_their_call_time_and_never_geocoded(self):
        incident = brpd._normalize_feature(CALL, now=NOW)
        with patch.object(app, 'geocode_address', side_effect=AssertionError('must not geocode')):
            app.process_incidents([copy.copy(incident)], source='brpd')
            app.process_incidents([copy.copy(incident)], source='brpd')
        conn = app.db_connect(row_factory=True)
        self.addCleanup(conn.close)
        rows = conn.execute('SELECT * FROM incidents').fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['source'], 'brpd')
        self.assertEqual(rows[0]['first_seen'], incident['occurred_at'])
        self.assertEqual(rows[0]['is_active'], 0)
        self.assertEqual((rows[0]['latitude'], rows[0]['longitude']), (30.451, -91.143))
        self.assertEqual(rows[0]['geocode_quality'], 'approximate-published')
        self.assertEqual(app._incident_reported_at(dict(rows[0])), incident['occurred_at'])

    def test_police_calls_do_not_end_live_traffic_incidents(self):
        conn = app.db_connect()
        conn.execute("INSERT INTO incidents(hash, source, is_active, first_seen) VALUES ('live', 'batonrouge', 1, ?)",
                     (datetime.now(timezone.utc).isoformat(),))
        conn.commit()
        conn.close()
        app.process_incidents([brpd._normalize_feature(CALL, now=NOW)], source='brpd')
        conn = app.db_connect()
        self.addCleanup(conn.close)
        self.assertEqual(conn.execute("SELECT is_active FROM incidents WHERE hash = 'live'").fetchone()[0], 1)

    def test_baton_rouge_tab_shows_live_traffic_with_a_bounded_slice_of_police_calls(self):
        conn = app.db_connect()
        recent = datetime.now(timezone.utc) - timedelta(days=2)
        for index in range(brpd.RECENT_LIMIT + 5):
            conn.execute('INSERT INTO incidents(hash, source, is_active, first_seen) VALUES (?,?,0,?)',
                         (f'brpd-{index}', 'brpd', (recent + timedelta(minutes=index)).isoformat()))
        conn.execute('INSERT INTO incidents(hash, source, is_active, first_seen) VALUES (?,?,0,?)',
                     ('too-old', 'brpd', (recent - timedelta(days=brpd.RECENT_DAYS + 3)).isoformat()))
        for name, source, active in (('traffic-now', 'batonrouge', 1), ('traffic-ended', 'batonrouge', 0), ('caddo-now', 'caddo', 1)):
            conn.execute('INSERT INTO incidents(hash, source, is_active, first_seen) VALUES (?,?,?,?)',
                         (name, source, active, recent.isoformat()))
        conn.commit()
        conn.close()
        with app.app.test_request_context('/api/incidents/active?source=batonrouge'):
            rows = app.get_active_incidents().get_json()
        self.assertEqual(len(rows), brpd.RECENT_LIMIT + 1)
        self.assertEqual({row['source'] for row in rows}, {'batonrouge', 'brpd'})
        hashes = {row['hash'] for row in rows}
        self.assertIn('traffic-now', hashes)
        self.assertIn(f'brpd-{brpd.RECENT_LIMIT + 4}', hashes)
        self.assertFalse({'traffic-ended', 'too-old', 'caddo-now', 'brpd-0'} & hashes)
        self.assertTrue(all(row['is_active'] == 0 for row in rows if row['source'] == 'brpd'))
        with app.app.test_request_context('/api/incidents/active?source=brpd'):
            self.assertEqual({row['source'] for row in app.get_active_incidents().get_json()}, {'brpd'})

    def test_baton_rouge_history_and_calendar_cover_both_feeds(self):
        conn = app.db_connect(row_factory=True)
        for name, source in (('traffic', 'batonrouge'), ('police', 'brpd'), ('cad', 'caddo')):
            conn.execute('INSERT INTO incidents(hash, source, is_active, first_seen) VALUES (?,?,0,?)',
                         (name, source, '2026-10-30T15:00:00+00:00'))
        conn.commit()
        bounds = app._central_date_bounds_utc('2026-10-30')
        rows, total = app._query_history_candidates(conn, bounds, 'batonrouge', 10)
        self.assertEqual(total, 2)
        self.assertEqual({row['source'] for row in rows}, {'batonrouge', 'brpd'})
        self.assertEqual(app._query_history_candidates(conn, bounds, 'brpd', 10)[1], 1)
        conn.close()
        with app.app.test_request_context('/api/incidents/history_counts?month=2026-10&source=batonrouge'), \
                patch.object(app, '_history_ui_request_guard', return_value=None), \
                patch.object(app, '_get_archive_dbs_for_month', return_value=[]):
            counts = app.get_history_counts().get_json()['counts']
        self.assertEqual(counts.get('2026-10-30'), 2)

    def test_source_names_and_shared_page_wording(self):
        self.assertEqual(app._normalize_source_filter('brpd'), 'brpd')
        self.assertEqual(app._normalize_source_name('brpd'), 'brpd')
        self.assertEqual(app._delayed_call_summary('brpd'), 'Baton Rouge police call published a day or two later')
        self.assertEqual(app._delayed_call_summary('sulphur'), 'Sulphur police call published with a 24-hour delay')

    def test_collector_failure_is_logged_and_returns_nothing(self):
        with patch.object(brpd, 'scrape', side_effect=ValueError('layer changed')), patch.object(app, 'log') as log:
            self.assertEqual(app.scrape_brpd_incidents(), ([], None))
        self.assertIn('layer changed', log.call_args.args[0])


if __name__ == '__main__':
    unittest.main()
