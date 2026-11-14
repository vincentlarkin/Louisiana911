import os
import tempfile
import time
import unittest
from unittest.mock import patch

# Other test modules share the imported app (and its database), so only set a
# path if none is set yet and leave cleanup to whoever owns it.
os.environ.setdefault(
    "LOUISIANA911_DB_PATH",
    os.path.join(tempfile.gettempdir(), f"louisiana911-app-session-tests-{os.getpid()}.db"),
)
import app
import app_integrity

CLIENT = {'X-Louisiana911-Client': 'android/0.2.0', 'Host': 'localhost'}


class AppSessionTests(unittest.TestCase):
    def setUp(self):
        self.client = app.app.test_client()
        with app._report_rate_lock:
            app._report_rate_hits.clear()
            app._history_date_rate_hits.clear()

    def token(self):
        response = self.client.post('/api/app/session', headers=CLIENT)
        self.assertEqual(200, response.status_code)
        self.assertIn('no-store', response.headers['Cache-Control'])
        return response.get_json()['token']

    def test_session_needs_client_header(self):
        self.assertEqual(404, self.client.post('/api/app/session', headers={'Host': 'localhost'}).status_code)
        bad = {'X-Louisiana911-Client': 'iphone/1.0', 'Host': 'localhost'}
        self.assertEqual(404, self.client.post('/api/app/session', headers=bad).status_code)

    def test_session_rejects_get_and_cross_site(self):
        self.assertIn(self.client.get('/api/app/session', headers=CLIENT).status_code, {404, 405})
        cross = dict(CLIENT, **{'Sec-Fetch-Site': 'cross-site'})
        self.assertEqual(403, self.client.post('/api/app/session', headers=cross).status_code)

    def test_token_opens_the_api_without_browser_headers(self):
        token = self.token()
        self.assertEqual(404, self.client.get('/api/incidents/active?source=all', headers=CLIENT).status_code)
        ok = self.client.get('/api/incidents/active?source=all',
                             headers=dict(CLIENT, **{'X-Louisiana911-App-Token': token}))
        self.assertEqual(200, ok.status_code)

    def test_token_opens_history_without_the_page_cookie(self):
        token = self.token()
        response = self.client.get('/api/incidents/history?date=2026-11-01',
                                   headers=dict(CLIENT, **{'X-Louisiana911-App-Token': token}))
        self.assertNotEqual(404, response.status_code)

    def test_bad_expired_or_tampered_tokens_get_401(self):
        token = self.token()
        tampered = token[:-2] + ('AA' if not token.endswith('AA') else 'BB')
        old = app._new_app_signed('a1', 'android')
        version, _, nonce, client, _ = old.split(':')
        stale_payload = f'{version}:{int(time.time()) - 90000}:{nonce}:{client}'
        stale = f'{stale_payload}:{app._app_signature(stale_payload)}'
        challenge = app._new_app_signed('c1', 'android')  # wrong kind
        for value in (tampered, 'nonsense', stale, challenge):
            response = self.client.get('/api/incidents/active',
                                       headers=dict(CLIENT, **{'X-Louisiana911-App-Token': value}))
            self.assertEqual(401, response.status_code, value)
            self.assertEqual('app_token_invalid', response.get_json()['error'])

    def test_token_cannot_write(self):
        token = self.token()
        response = self.client.post('/api/incidents/active',
                                    headers=dict(CLIENT, **{'X-Louisiana911-App-Token': token}))
        self.assertEqual(401, response.status_code)

    def test_off_mode_disables_everything(self):
        token = self.token()
        with patch.object(app, 'APP_VERIFY_MODE', 'off'):
            self.assertEqual(404, self.client.post('/api/app/session', headers=CLIENT).status_code)
            response = self.client.get('/api/incidents/active',
                                       headers=dict(CLIENT, **{'X-Louisiana911-App-Token': token}))
            self.assertEqual(401, response.status_code)

    def test_session_is_rate_limited(self):
        with patch.object(app, 'APP_SESSION_RATE_LIMIT', 2):
            codes = [self.client.post('/api/app/session', headers=CLIENT).status_code for _ in range(3)]
        self.assertEqual([200, 200, 429], codes)


class PlayIntegrityModeTests(unittest.TestCase):
    def setUp(self):
        self.client = app.app.test_client()
        with app._report_rate_lock:
            app._report_rate_hits.clear()
        patcher = patch.multiple(app, APP_VERIFY_MODE='play-integrity', PLAY_INTEGRITY_CREDENTIALS='/x.json')
        patcher.start()
        self.addCleanup(patcher.stop)

    def verdict(self, challenge, **overrides):
        payload = {
            'requestDetails': {
                'requestPackageName': 'com.louisiana911.app',
                'requestHash': app_integrity.request_hash(challenge),
                'timestampMillis': str(int(time.time() * 1000)),
            },
            'appIntegrity': {'appRecognitionVerdict': 'PLAY_RECOGNIZED'},
            'deviceIntegrity': {'deviceRecognitionVerdict': ['MEETS_DEVICE_INTEGRITY']},
        }
        for key, value in overrides.items():
            payload[key] = value
        return payload

    def test_without_verdict_returns_challenge(self):
        response = self.client.post('/api/app/session', headers=CLIENT)
        self.assertEqual(401, response.status_code)
        body = response.get_json()
        self.assertEqual('integrity_required', body['error'])
        self.assertTrue(body['challenge'].startswith('c1:'))

    def test_good_verdict_gets_token(self):
        challenge = self.client.post('/api/app/session', headers=CLIENT).get_json()['challenge']
        with patch.object(app_integrity, 'decode', return_value=self.verdict(challenge)) as decode:
            response = self.client.post('/api/app/session', headers=CLIENT,
                                        json={'challenge': challenge, 'integrityToken': 'abc'})
        self.assertEqual(200, response.status_code)
        decode.assert_called_once_with('com.louisiana911.app', 'abc', '/x.json')

    def test_sideloaded_app_is_refused(self):
        challenge = self.client.post('/api/app/session', headers=CLIENT).get_json()['challenge']
        verdict = self.verdict(challenge, appIntegrity={'appRecognitionVerdict': 'UNRECOGNIZED_VERSION'})
        with patch.object(app_integrity, 'decode', return_value=verdict):
            response = self.client.post('/api/app/session', headers=CLIENT,
                                        json={'challenge': challenge, 'integrityToken': 'abc'})
        self.assertEqual(401, response.status_code)
        self.assertEqual('unrecognized_app', response.get_json()['error'])

    def test_verdict_for_another_challenge_is_refused(self):
        challenge = self.client.post('/api/app/session', headers=CLIENT).get_json()['challenge']
        other = app._new_app_signed('c1', 'android')
        with patch.object(app_integrity, 'decode', return_value=self.verdict(other)):
            response = self.client.post('/api/app/session', headers=CLIENT,
                                        json={'challenge': challenge, 'integrityToken': 'abc'})
        self.assertEqual('wrong_request', response.get_json()['error'])

    def test_google_outage_is_503(self):
        challenge = self.client.post('/api/app/session', headers=CLIENT).get_json()['challenge']
        with patch.object(app_integrity, 'decode', side_effect=OSError('down')):
            response = self.client.post('/api/app/session', headers=CLIENT,
                                        json={'challenge': challenge, 'integrityToken': 'abc'})
        self.assertEqual(503, response.status_code)


if __name__ == '__main__':
    unittest.main()
