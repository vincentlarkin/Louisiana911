import os
import tempfile
import time
import unittest
from unittest.mock import patch

os.environ.setdefault(
    "LOUISIANA911_DB_PATH",
    os.path.join(tempfile.gettempdir(), f"louisiana911-weather-tests-{os.getpid()}.db"),
)
import app
import weather

CLIENT = {'X-Louisiana911-Client': 'android/0.3.3', 'Host': 'localhost'}


def observation(celsius=11, icon='https://api.weather.gov/icons/land/night/bkn?size=medium', age=600):
    stamp = time.strftime('%Y-%m-%dT%H:%M:%S+00:00', time.gmtime(time.time() - age))
    return {'properties': {'temperature': {'value': celsius}, 'timestamp': stamp,
                           'icon': icon, 'textDescription': 'Mostly Cloudy'}}


class WeatherTests(unittest.TestCase):
    def test_sky_words(self):
        base = 'https://api.weather.gov/icons/land/'
        self.assertEqual('clear', weather.sky_from_icon(base + 'day/skc?size=medium'))
        self.assertEqual('clear-night', weather.sky_from_icon(base + 'night/skc'))
        self.assertEqual('partly', weather.sky_from_icon(base + 'day/sct'))
        self.assertEqual('cloudy', weather.sky_from_icon(base + 'night/bkn?size=medium'))
        self.assertEqual('fog', weather.sky_from_icon(base + 'day/fg'))
        self.assertEqual('rain', weather.sky_from_icon(base + 'day/rain_showers,40'))
        self.assertEqual('storm', weather.sky_from_icon(base + 'day/tsra,60'))
        self.assertEqual('unknown', weather.sky_from_icon(None))

    def test_summary_converts_and_drops_stale(self):
        s = weather.summarize(observation(celsius=11))
        self.assertEqual(52, s['tempF'])
        self.assertEqual('cloudy', s['sky'])
        self.assertIsNone(weather.summarize(observation(age=3 * 3600)))
        self.assertIsNone(weather.summarize({'properties': {'temperature': {'value': None}}}))

    def test_endpoint_is_small_and_app_only(self):
        areas = {'caddo': {'tempF': 52, 'sky': 'cloudy', 'text': 'Mostly Cloudy', 'at': 'x'}}
        client = app.app.test_client()
        with app._report_rate_lock:
            app._report_rate_hits.clear()
        with patch.object(weather, 'current', return_value=areas):
            self.assertEqual(404, client.get('/api/weather', headers=CLIENT).status_code)
            for kind in ('a1', 'a2'):
                token = app._new_app_signed(kind, 'android')
                r = client.get('/api/weather', headers=dict(CLIENT, **{'X-Louisiana911-App-Token': token}))
                self.assertEqual(200, r.status_code)
                self.assertEqual(areas, r.get_json()['areas'])
                self.assertLess(len(r.data), 600)

    def test_refresh_backs_off_after_failure(self):
        with patch.object(weather, '_cache', {'at': 0.0, 'areas': {}}), \
             patch.object(weather.requests, 'get', side_effect=weather.requests.RequestException('down')) as get:
            weather._refresh()
            self.assertEqual(len(weather.STATIONS), get.call_count)
            self.assertGreater(weather._cache['at'], 0)
            with patch.object(weather, 'Thread') as thread:
                weather.current()
                thread.assert_not_called()


if __name__ == '__main__':
    unittest.main()
