import gzip
import json
import os
import tempfile
import unittest
from unittest.mock import patch

os.environ.setdefault(
    "LOUISIANA911_DB_PATH",
    os.path.join(tempfile.gettempdir(), f"louisiana911-active-cache-tests-{os.getpid()}.db"),
)
import app

BROWSER = {'Sec-Fetch-Site': 'same-origin', 'Sec-Fetch-Mode': 'cors', 'Host': 'localhost'}


class ActiveCacheTests(unittest.TestCase):
    def setUp(self):
        self.client = app.app.test_client()
        app._active_cache.clear()
        with app._report_rate_lock:
            app._report_rate_hits.clear()

    def test_built_once_per_scrape_and_gzipped(self):
        calls = [{'id': 1, 'description': 'TEST'}]
        with patch.object(app, '_active_incident_payload', return_value=calls) as build, \
             patch.object(app, '_scrape_stamp', return_value='s1'):
            plain = self.client.get('/api/incidents/active', headers=BROWSER)
            zipped = self.client.get('/api/incidents/active', headers=dict(BROWSER, **{'Accept-Encoding': 'gzip'}))
            self.assertEqual(1, build.call_count)
            self.assertEqual(calls, json.loads(plain.data))
            self.assertEqual('gzip', zipped.headers['Content-Encoding'])
            self.assertEqual(calls, json.loads(gzip.decompress(zipped.data)))
            self.assertEqual(plain.headers['ETag'], zipped.headers['ETag'])
            self.assertEqual('public, max-age=0, s-maxage=15', plain.headers['Cache-Control'])

    def test_new_scrape_rebuilds(self):
        with patch.object(app, '_active_incident_payload', return_value=[]) as build:
            with patch.object(app, '_scrape_stamp', return_value='s1'):
                self.client.get('/api/incidents/active', headers=BROWSER)
            with patch.object(app, '_scrape_stamp', return_value='s2'):
                self.client.get('/api/incidents/active', headers=BROWSER)
            self.assertEqual(2, build.call_count)

    def test_unchanged_poll_gets_304(self):
        with patch.object(app, '_active_incident_payload', return_value=[{'id': 2}]), \
             patch.object(app, '_scrape_stamp', return_value='s1'):
            first = self.client.get('/api/incidents/active', headers=BROWSER)
            again = self.client.get('/api/incidents/active', headers=dict(BROWSER, **{'If-None-Match': first.headers['ETag']}))
            self.assertEqual(304, again.status_code)
            self.assertEqual(b'', again.data)

    def test_guard_still_applies_and_refusals_are_never_cached(self):
        refused = self.client.get('/api/incidents/active', headers={'Host': 'localhost'})
        self.assertEqual(404, refused.status_code)
        self.assertIn('no-store', refused.headers['Cache-Control'])

    def test_other_api_stays_private(self):
        r = self.client.get('/api/status', headers=BROWSER)
        self.assertIn('no-store', r.headers['Cache-Control'])


if __name__ == '__main__':
    unittest.main()
