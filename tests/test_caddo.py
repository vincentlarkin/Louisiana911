import os
import tempfile
import unittest
from datetime import datetime, timezone
from unittest.mock import Mock, patch

os.environ.setdefault('LOUISIANA911_DB_PATH', os.path.join(tempfile.gettempdir(), f'caddo-tests-{os.getpid()}.db'))
import app
from sources import caddo


HEADERS = '<tr>' + ''.join(f'<th>{name}</th>' for name in
    ['Agency', 'Time', 'Units', 'Description', 'Street', 'Cross Streets', 'Mun']) + '</tr>'
ROW = '<tr><td>SPD</td><td>2350</td><td>1</td><td>ASSIST</td><td>MAIN</td><td>OAK</td><td>SHV</td></tr>'


class CaddoTests(unittest.TestCase):
    def scrape(self, body):
        response = Mock(text=body, url=caddo.BASE_URL, status_code=200)
        with patch.object(caddo.requests, 'Session') as session:
            session.return_value.get.return_value = response
            return caddo.scrape(user_agent='test')

    def page(self, rows=''):
        return '<p>Refreshed at: September 05 00:10</p><table id="ctl00_MainContent_GV_AE_ALL_P">' + HEADERS + rows + '</table>'

    def test_valid_snapshot_and_empty_table_are_recognized(self):
        rows, refreshed = self.scrape(self.page(ROW))
        self.assertEqual((len(rows), rows[0]['time']), (1, '2350'))
        self.assertTrue(refreshed)
        self.assertEqual(self.scrape(self.page()), ([], refreshed))
        nested = self.page(ROW).replace('Refreshed at: ', '<b>Refreshed at: </b>')
        self.assertEqual(self.scrape(nested), (rows, refreshed))

    def test_missing_or_malformed_feed_cannot_be_mistaken_for_empty(self):
        for body in ['<html>Unavailable</html>', self.page().replace('Agency', 'Changed'),
                     self.page(ROW.replace('<td>1</td>', '')),
                     self.page(ROW.replace('2350', '2560')),
                     self.page(ROW + ROW.replace('2350', 'unknown'))]:
            with self.subTest(body=body), self.assertRaises(ValueError):
                self.scrape(body)

    def test_reported_date_uses_first_observation_not_last_seen(self):
        cases = [
            ('2350', '2026-09-05T05:16:47+00:00', '2026-09-05T04:50:00+00:00'),
            ('2350', '2026-09-05T04:51:00+00:00', '2026-09-05T04:50:00+00:00'),
            ('0010', '2026-09-05T05:11:00+00:00', '2026-09-05T05:10:00+00:00'),
            ('0000', '2026-09-05T05:00:47+00:00', '2026-09-05T05:00:00+00:00'),
            ('2359', '2026-01-01T06:01:00+00:00', '2026-01-01T05:59:00+00:00'),
            ('2350', '2026-03-09T05:01:00+00:00', '2026-03-09T04:50:00+00:00'),
            # Repeated fall-back hour: choose an occurrence before observation.
            ('0150', '2026-11-01T07:10:00+00:00', '2026-11-01T06:50:00+00:00'),
            ('0110', '2026-11-01T07:15:00+00:00', '2026-11-01T07:10:00+00:00'),
        ]
        for clock, first, expected in cases:
            call = dict(source='caddo', time=clock, first_seen=first,
                        last_seen='2026-11-03T12:00:00+00:00')
            with self.subTest(clock=clock, first=first):
                self.assertEqual(app._public_incident(call)['reported_at'], expected)
                self.assertEqual(app._incident_share_payload(call)['reported_at'], expected)
                self.assertEqual(call['first_seen'], first)

    def test_missing_or_invalid_time_does_not_invent_a_date(self):
        for clock in [None, '', 'garbage', '2400', '1260']:
            self.assertIsNone(app._incident_reported_at(dict(time=clock, first_seen='2026-09-05T12:00:00Z')))

    def test_missing_calls_move_to_history_and_empty_snapshot_is_source_scoped(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(app, 'DB_PATH', os.path.join(directory, 'live.db')):
            app.init_db()
            original = dict(source='caddo', agency='SPD', time='2350', units=1, description='ASSIST',
                            street='MAIN', cross_streets='OAK', municipality='SHV', latitude=32.5, longitude=-93.75)
            second = {**original, 'time': '0010', 'street': 'SECOND'}
            with patch.object(app, 'datetime', wraps=datetime) as clock:
                clock.now.return_value = datetime(2026, 9, 5, 5, 16, tzinfo=timezone.utc)
                app.process_incidents([original, second])
            app.process_incidents([second])
            conn = app.db_connect(row_factory=True)
            try:
                rows = [dict(r) for r in conn.execute('SELECT * FROM incidents ORDER BY time')]
                self.assertEqual([r['is_active'] for r in rows], [1, 0])
                self.assertEqual(rows[1]['first_seen'], '2026-09-05T05:16:00+00:00')
                conn.execute("INSERT INTO incidents(hash,source,is_active) VALUES ('other','lafayette',1)")
                conn.commit()
                app.process_incidents([])  # unknown/failure must preserve active calls
                self.assertEqual(conn.execute('SELECT SUM(is_active) FROM incidents').fetchone()[0], 2)
                app.process_incidents([], allow_empty=True)
                self.assertEqual(conn.execute('SELECT SUM(is_active) FROM incidents').fetchone()[0], 1)
                history, count = app._query_history_candidates(conn, app._central_date_bounds_utc('2026-09-05'), 'caddo', 10)
                self.assertEqual(count, 2)
                self.assertEqual(len(history), 2)
            finally:
                conn.close()

    def test_background_collector_passes_only_validated_caddo_empty_snapshots(self):
        with patch.object(app, 'meta_set'), patch.object(app, '_store_feed_refresh'), \
             patch.dict(app.source_last_scrape_monotonic, {}, clear=True), \
             patch.object(app, 'scrape_batonrouge_incidents', return_value=(None, None)), \
             patch.object(app, 'scrape_lafayette_incidents', return_value=(None, None)), \
             patch.object(app, 'scrape_neworleans_incidents', return_value=([], None)), \
             patch.object(app, 'scrape_lakecharles_incidents', return_value=([], None)), \
             patch.object(app, 'process_incidents') as process:
            with patch.object(app, 'scrape_caddo_incidents', return_value=([], 'September 05 00:10')):
                app.background_scrape()
                process.assert_called_once_with([], source='caddo', allow_empty=True)
            process.reset_mock()
            app.source_last_scrape_monotonic.clear()
            with patch.object(app, 'scrape_caddo_incidents', return_value=(None, None)):
                app.background_scrape()
                process.assert_not_called()
