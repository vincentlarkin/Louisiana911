import json
import os
import tempfile
import unittest
from unittest.mock import patch

os.environ.setdefault('LOUISIANA911_DB_PATH', os.path.join(tempfile.gettempdir(), f'louisiana911-report-tests-{os.getpid()}.db'))
import app

HEADERS = {'Host': 'localhost', 'Sec-Fetch-Site': 'same-origin', 'Sec-Fetch-Mode': 'cors'}


class SeverityTests(unittest.TestCase):
    def test_matches_map_severity_cases(self):
        with open(os.path.join(os.path.dirname(__file__), 'severity_cases.json'), encoding='utf-8') as fixture:
            cases = json.load(fixture)
        for case in cases:
            with self.subTest(case=case):
                self.assertEqual(case['severity'], app.incident_severity(case['description'], case['agency']))


class ReportApiTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        for patcher in (
            patch.object(app, 'DB_PATH', os.path.join(self.directory.name, 'live.db')),
            patch.object(app, 'REPORT_RATE_WINDOW_SECONDS', 0),
            patch.dict(app._report_cache, clear=True),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        app.init_db()
        self.client = app.app.test_client()

    def insert(self, conn, key, first_seen, description, *, source='caddo', agency='SPD',
               lat=32.5, lon=-93.75, street='MAIN ST'):
        conn.execute(
            'INSERT INTO incidents(hash, first_seen, source, agency, description, street, latitude, longitude, is_active) '
            'VALUES (?,?,?,?,?,?,?,?,0)',
            (key, first_seen, source, agency, description, street, lat, lon),
        )

    def get(self, url):
        response = self.client.get(url, headers=HEADERS)
        return response.status_code, response.get_json()

    def test_summary_counts_each_source_by_severity_across_archive_and_live(self):
        archive_path = app._get_archive_db_path(2026, 8)
        app._init_archive_db(archive_path)
        archive = app._archive_db_connect(archive_path)
        self.insert(archive, 'a1', '2026-08-03T15:00:00+00:00', 'SHOTS FIRED')
        self.insert(archive, 'a2', '2026-08-04T15:00:00+00:00', 'TRAFFIC STOP')
        self.insert(archive, 'dup', '2026-08-05T15:00:00+00:00', 'TRAFFIC STOP')
        archive.commit()
        archive.close()
        live = app.db_connect()
        self.insert(live, 'dup', '2026-08-05T15:00:00+00:00', 'TRAFFIC STOP')
        self.insert(live, 'b1', '2026-08-06T15:00:00+00:00', 'CRASH', source='batonrouge', agency='LAW')
        self.insert(live, 'july', '2026-07-30T15:00:00+00:00', 'THEFT')
        live.commit()
        live.close()

        status, body = self.get('/api/reports/summary?month=2026-08')
        self.assertEqual(200, status)
        by_source = {source['key']: source for source in body['sources']}
        self.assertEqual(3, by_source['caddo']['total'])
        self.assertEqual(1, by_source['caddo']['previousTotal'])
        self.assertEqual(1, by_source['caddo']['severity']['high'])
        self.assertEqual(2, by_source['caddo']['severity']['low'])
        self.assertEqual({'description': 'TRAFFIC STOP', 'count': 2, 'severity': 'low'}, by_source['caddo']['types'][0])
        self.assertEqual(1, by_source['batonrouge']['severity']['medium'])
        self.assertIn('2026-07', body['months'])

        self.assertEqual(404, self.get('/api/reports/summary?month=2020-01')[0])
        self.assertEqual(400, self.get('/api/reports/summary?month=August')[0])

    def test_nearby_counts_only_mapped_incidents_inside_the_radius(self):
        live = app.db_connect()
        recent = '2026-10-20T15:00:00+00:00'
        self.insert(live, 'near', recent, 'SHOTS FIRED', lat=32.5005, lon=-93.75)        # ~180 ft
        self.insert(live, 'edge', recent, 'TRAFFIC STOP', lat=32.5030, lon=-93.75)       # ~0.21 mi
        self.insert(live, 'far', recent, 'THEFT', lat=32.52, lon=-93.75)                 # ~1.4 mi
        self.insert(live, 'unknown', recent, 'THEFT', street='UNKNOWN', lat=32.5, lon=-93.75)
        self.insert(live, 'old', '2025-01-01T15:00:00+00:00', 'THEFT', lat=32.5, lon=-93.75)
        live.commit()
        live.close()

        with patch.object(app, 'datetime', wraps=app.datetime) as clock:
            clock.now.side_effect = lambda tz=None: app.datetime(2026, 10, 25, 12, tzinfo=tz or app.timezone.utc)
            status, body = self.get('/api/reports/nearby?lat=32.5&lon=-93.75&radius=0.1&days=30')
            self.assertEqual(200, status)
            self.assertEqual(1, body['total'])
            self.assertEqual('SHOTS FIRED', body['incidents'][0]['description'])
            self.assertEqual(1, body['severity']['high'])

            status, body = self.get('/api/reports/nearby?lat=32.5&lon=-93.75&radius=0.25&days=30')
            self.assertEqual(2, body['total'])

            status, body = self.get('/api/reports/nearby?lat=32.5&lon=-93.75&radius=2&days=0')
            self.assertEqual(4, body['total'])
            self.assertEqual('MAIN ST', body['blocks'][0]['label'])
            self.assertEqual(4, body['blocks'][0]['count'])
            self.assertEqual(3, body['blocks'][0]['severity']['high'])  # shots fired + two thefts

    def test_nearby_rejects_unsupported_input(self):
        for query in (
            'lat=32.5&lon=-93.75&radius=0.3&days=30',
            'lat=32.5&lon=-93.75&radius=0.25&days=7',
            'lat=40.7&lon=-74.0&radius=0.25&days=30',
            'lon=-93.75&radius=0.25&days=30',
        ):
            with self.subTest(query=query):
                self.assertEqual(400, self.get('/api/reports/nearby?' + query)[0])

    def test_geocode_filters_results_outside_coverage(self):
        class Place:
            def __init__(self, address, lat, lon):
                self.address, self.latitude, self.longitude = address, lat, lon

        places = [Place('1500 Line Ave, Shreveport', 32.4977, -93.7467), Place('Somewhere, NY', 40.7, -74.0)]
        with patch.object(app.geolocator_arcgis, 'geocode', return_value=places) as geocode, \
                patch.dict(app._report_geocode_cache, clear=True):
            status, body = self.get('/api/reports/geocode?q=1500+Line+Ave+Shreveport')
            self.assertEqual(200, status)
            self.assertEqual(['1500 Line Ave, Shreveport'], [r['label'] for r in body['results']])
            self.assertEqual('1500 Line Ave Shreveport, Louisiana', geocode.call_args[0][0])
        self.assertEqual(400, self.get('/api/reports/geocode?q=ab')[0])

    def test_monthly_report_page_redirects_to_reports(self):
        for path in ('/reports/monthly/', '/reports/monthly'):
            response = self.client.get(path, headers={'Host': 'localhost'})
            self.assertEqual(301, response.status_code)
            self.assertTrue(response.headers['Location'].endswith('/reports/'))


if __name__ == '__main__':
    unittest.main()
