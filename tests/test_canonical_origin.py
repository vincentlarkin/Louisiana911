import os
import tempfile
import unittest
from unittest.mock import patch

_IMPORT_DB_PATH = os.path.join(
    tempfile.gettempdir(), f"louisiana911-canonical-tests-{os.getpid()}.db"
)
os.environ["LOUISIANA911_DB_PATH"] = _IMPORT_DB_PATH
import app


def tearDownModule():
    for suffix in ("", "-shm", "-wal"):
        try:
            os.remove(_IMPORT_DB_PATH + suffix)
        except FileNotFoundError:
            pass


class CanonicalOriginTests(unittest.TestCase):
    def setUp(self):
        self.client = app.app.test_client()
        with app._report_rate_lock:
            app._report_rate_hits.clear()
            app._history_date_rate_hits.clear()

    def test_proxy_http_redirects_to_https_apex_and_preserves_query(self):
        response = self.client.get(
            '/reports/?month=2026-07',
            headers={
                'Host': 'louisiana911.com',
                'X-Forwarded-Proto': 'http',
            },
        )

        self.assertEqual(301, response.status_code)
        self.assertEqual(
            'https://louisiana911.com/reports/?month=2026-07',
            response.headers['Location'],
        )

    def test_www_redirects_to_apex_even_when_proxy_uses_https(self):
        response = self.client.get(
            '/about/',
            headers={
                'Host': 'www.louisiana911.com',
                'X-Forwarded-Proto': 'https',
            },
        )

        self.assertEqual(301, response.status_code)
        self.assertEqual(
            'https://louisiana911.com/about/',
            response.headers['Location'],
        )

    def test_https_apex_is_not_redirected(self):
        response = self.client.get(
            '/',
            headers={
                'Host': 'louisiana911.com',
                'X-Forwarded-Proto': 'https',
            },
        )

        self.assertEqual(200, response.status_code)

    def test_local_development_host_is_not_redirected(self):
        response = self.client.get('/', headers={'Host': 'localhost'})

        self.assertEqual(200, response.status_code)

    def test_map_defaults_to_labeled_streets_with_dark_and_light_appearances(self):
        response = self.client.get('/', headers={'Host': 'localhost'})
        html = response.get_data(as_text=True)

        self.assertNotIn('World_Dark_Gray_Base', html)
        self.assertIn('https://tile.openstreetmap.org/{z}/{x}/{y}.png', html)
        self.assertIn("storedBasemapMode === 'light' ? 'light' : 'dark'", html)
        self.assertIn('data-basemap="dark"', html)
        self.assertIn('data-basemap="light"', html)
        self.assertIn('const cartoLayers = cartoKey ?', html)
        self.assertIn('dark_all/{z}/{x}/{y}{r}.png?key=', html)
        self.assertIn('dark-matter-gl-style/style.json', html)
        self.assertIn('positron-gl-style/style.json', html)

    def test_basemap_key_uses_local_config_and_environment_override(self):
        with tempfile.TemporaryDirectory() as instance_path, \
                patch.object(app.app, 'instance_path', instance_path), \
                patch.dict(os.environ, {'LOUISIANA911_CARTO_BASEMAP_KEY': '', 'CARTO_BASEMAP_API_KEY': ''}):
            self.assertEqual('', app._carto_basemap_key())
            config_path = os.path.join(instance_path, 'basemaps.json')
            with open(config_path, 'w') as config_file:
                config_file.write('{"cartoKey":"local-key"}')
            self.assertEqual('local-key', app._carto_basemap_key())
            with patch.dict(os.environ, {'LOUISIANA911_CARTO_BASEMAP_KEY': 'deployment-key'}):
                self.assertEqual('deployment-key', app._carto_basemap_key())
            with open(config_path, 'w') as config_file:
                config_file.write('invalid JSON')
            self.assertEqual('', app._carto_basemap_key())

    def test_basemap_config_preserves_key_and_escapes_script_markup(self):
        import json
        key = 'test-key</script><script>alert(1)</script>'
        with patch.dict(os.environ, {'LOUISIANA911_CARTO_BASEMAP_KEY': key}):
            html = self.client.get('/', headers={'Host': 'localhost'}).get_data(as_text=True)
        payload = html.split('id="map-config">', 1)[1].split('</script>', 1)[0]
        self.assertNotIn('<', payload)
        self.assertEqual(key, json.loads(payload)['cartoKey'])

    def test_incident_markers_have_a_fixed_visible_symbol_above_basemaps(self):
        response = self.client.get('/', headers={'Host': 'localhost'})
        html = response.get_data(as_text=True)

        self.assertIn("map.createPane('incident')", html)
        self.assertIn("map.getPane('incident').style.zIndex = 675", html)
        self.assertIn("className: 'incident-map-marker'", html)
        self.assertNotIn('L.polygon(', html)
        self.assertIn('incident-circle-symbol', html)

    def test_cross_origin_tiles_receive_origin_referrer(self):
        response = self.client.get('/', headers={'Host': 'localhost'})

        self.assertEqual(
            'strict-origin-when-cross-origin',
            response.headers['Referrer-Policy'],
        )

    def test_coverage_pages_are_public_html(self):
        expected_pages = {
            '/coverage/': 'Louisiana 911 Coverage by City and Parish',
            '/caddo911/': 'Caddo 911 Live Calls',
            '/coverage/baton-rouge/': 'Baton Rouge Traffic Incidents',
            '/coverage/lafayette/': 'Lafayette Parish Traffic Incidents',
            '/coverage/new-orleans/': 'New Orleans NOPD Calls for Service',
        }

        for path, expected_title in expected_pages.items():
            with self.subTest(path=path):
                response = self.client.get(path, headers={'Host': 'localhost'})
                self.assertEqual(200, response.status_code)
                self.assertIn('text/html', response.content_type)
                self.assertIn(expected_title, response.get_data(as_text=True))

    def test_coverage_routes_without_slashes_redirect_permanently(self):
        for path in (
            '/coverage',
            '/coverage/baton-rouge',
            '/coverage/lafayette',
            '/coverage/new-orleans',
        ):
            with self.subTest(path=path):
                response = self.client.get(path, headers={'Host': 'localhost'})
                self.assertEqual(301, response.status_code)
                self.assertEqual(f'{path}/', response.headers['Location'])

    def test_versioned_shell_assets_are_immutable(self):
        for path in (
            '/analytics.js?v=4.3.0',
            '/styles.css?v=4.3.6',
            '/service-worker.js?v=4.3.6',
            '/manifest.webmanifest?v=4.3.6',
        ):
            with self.subTest(path=path):
                response = self.client.get(path, headers={'Host': 'localhost'})
                self.assertEqual(200, response.status_code)
                self.assertEqual(
                    'public, max-age=31536000, immutable',
                    response.headers['Cache-Control'],
                )

    def test_html_is_not_marked_immutable(self):
        response = self.client.get('/coverage/', headers={'Host': 'localhost'})

        self.assertEqual(200, response.status_code)
        self.assertNotIn('immutable', response.headers.get('Cache-Control', ''))

    def test_every_public_page_uses_shared_deferred_analytics(self):
        for path in (
            '/',
            '/about/',
            '/caddo911/',
            '/coverage/',
            '/coverage/baton-rouge/',
            '/coverage/lafayette/',
            '/coverage/new-orleans/',
            '/reports/',
            '/reports/monthly/',
        ):
            with self.subTest(path=path):
                response = self.client.get(path, headers={'Host': 'localhost'})
                self.assertEqual(200, response.status_code)
                html = response.get_data(as_text=True)
                self.assertEqual(1, html.count('/analytics.js?v=4.3.0'))
                self.assertIn('<script defer src="/analytics.js?v=4.3.0"></script>', html)
                self.assertNotIn('googletagmanager.com/gtag/js', html)

    def test_analytics_bundle_includes_key_engagement_events(self):
        response = self.client.get('/analytics.js?v=4.3.0', headers={'Host': 'localhost'})
        self.assertEqual(200, response.status_code)
        javascript = response.get_data(as_text=True)
        for event_name in (
            'ui_click',
            'control_change',
            'scroll_depth',
            'container_scroll_depth',
            'section_view',
            'engagement_milestone',
            'page_summary',
            'page_performance',
            'web_vital',
            'client_error',
        ):
            with self.subTest(event_name=event_name):
                self.assertIn(f"'{event_name}'", javascript)

    def test_map_markers_include_mobile_tap_target_and_incident_dialog(self):
        response = self.client.get('/', headers={'Host': 'localhost'})
        html = response.get_data(as_text=True)

        self.assertEqual(200, response.status_code)
        self.assertIn("iconSize: [36, 36]", html)
        self.assertIn("keyboard: true", html)
        self.assertIn("openIncidentDialog(activeIncident", html)
        self.assertIn("const marker = L.marker(incidentCoordinatePair(incident)", html)

    def test_incident_share_route_has_breaking_metadata_and_embedded_incident(self):
        share_id = '0123456789abcdef0123456789abcdef'
        conn = app.db_connect()
        try:
            conn.execute(
                '''
                INSERT INTO incidents (
                    hash, agency, time, units, description, street, cross_streets,
                    municipality, source, latitude, longitude, first_seen, last_seen, is_active
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''',
                (
                    share_id, 'SFD', '1430', 8, 'MASS CASUALTY INCIDENT',
                    '100 TEST ST', 'SAMPLE AVE', 'Shreveport', 'caddo',
                    32.5252, -93.7502, '2026-08-29T19:30:00+00:00',
                    '2026-08-29T19:35:00+00:00', 1,
                ),
            )
            conn.commit()
        finally:
            conn.close()

        self.addCleanup(self._delete_incident, share_id)
        response = self.client.get(f'/incident/{share_id}', headers={'Host': 'localhost'})
        html = response.get_data(as_text=True)

        self.assertEqual(200, response.status_code)
        self.assertIn('BREAKING - VERY ACTIVE: MASS CASUALTY INCIDENT', html)
        self.assertIn(
            f'<link rel="canonical" href="https://louisiana911.com/incident/{share_id}">',
            html,
        )
        self.assertIn('<meta property="og:type" content="article">', html)
        self.assertIn('<base href="/">', html)
        self.assertIn('<meta name="robots" content="noindex,follow,max-image-preview:large">', html)
        self.assertIn('id="shared-incident-data"', html)
        self.assertIn(f'"share_id":"{share_id}"', html)
        self.assertIn('"is_active":1', html)

        conn = app.db_connect()
        try:
            conn.execute('UPDATE incidents SET is_active = 0 WHERE hash = ?', (share_id,))
            conn.commit()
        finally:
            conn.close()
        historical = self.client.get(f'/incident/{share_id}', headers={'Host': 'localhost'})
        historical_html = historical.get_data(as_text=True)
        self.assertIn('INCIDENT REPORT: MASS CASUALTY INCIDENT', historical_html)
        self.assertNotIn('BREAKING - VERY ACTIVE: MASS CASUALTY INCIDENT', historical_html)
        self.assertIn('"is_active":0', historical_html)

    def test_missing_or_malformed_incident_share_link_returns_404(self):
        for share_id in ('not-an-incident', 'f' * 32):
            with self.subTest(share_id=share_id):
                response = self.client.get(f'/incident/{share_id}', headers={'Host': 'localhost'})
                self.assertEqual(404, response.status_code)

    def test_ui_has_incident_share_controls_and_mass_casualty_override(self):
        response = self.client.get('/', headers={'Host': 'localhost'})
        html = response.get_data(as_text=True)

        self.assertIn('id="incident-modal-share"', html)
        self.assertIn('id="incident-modal-copy"', html)
        self.assertIn('shareIncidentByShareId', html)
        self.assertIn("'mass casualty'", html)
        self.assertIn("_includesWholeTerm(d, 'mci')", html)
        self.assertIn("incident._severity = 'high'", html)

    @staticmethod
    def _delete_incident(share_id):
        conn = app.db_connect()
        try:
            conn.execute('DELETE FROM incidents WHERE hash = ?', (share_id,))
            conn.commit()
        finally:
            conn.close()

    def test_history_rate_notice_is_connected_to_history_requests(self):
        response = self.client.get('/', headers={'Host': 'localhost'})
        html = response.get_data(as_text=True)

        self.assertEqual(200, response.status_code)
        self.assertIn('id="history-request-notice"', html)
        self.assertIn("if (response.status === 429 && showLimitNotice)", html)
        self.assertIn("response.headers.get('Retry-After')", html)
        self.assertIn('showHistoryRequestNotice(await getHistoryRetrySeconds(response))', html)
        self.assertIn('response.status === 404 && retrySession', html)

    def test_history_limit_counts_distinct_days_not_calendar_requests(self):
        headers = {
            'Host': 'localhost',
            'Sec-Fetch-Site': 'same-origin',
            'Sec-Fetch-Mode': 'cors',
            'User-Agent': 'Distinct History Day Test',
            'X-Louisiana911-UI': 'history',
        }
        self.client.get(
            '/',
            headers={
                **headers,
                'Sec-Fetch-Site': 'none',
                'Sec-Fetch-Mode': 'navigate',
                'Sec-Fetch-Dest': 'document',
            },
        )

        with patch.object(app, 'INCIDENT_HISTORY_API_RATE_LIMIT', 2):
            for month in ('2026-05', '2026-06', '2026-07'):
                counts = self.client.get(
                    f'/api/incidents/history_counts?month={month}',
                    headers=headers,
                )
                self.assertEqual(200, counts.status_code)

            for _ in range(4):
                repeated = self.client.get(
                    '/api/incidents/history?date=2026-07-18',
                    headers=headers,
                )
                self.assertEqual(200, repeated.status_code)

            second_day = self.client.get(
                '/api/incidents/history?date=2026-07-19',
                headers=headers,
            )
            third_day = self.client.get(
                '/api/incidents/history?date=2026-07-20',
                headers=headers,
            )

        self.assertEqual(200, second_day.status_code)
        self.assertEqual(429, third_day.status_code)

    def test_approximate_locations_use_concise_copy(self):
        response = self.client.get('/', headers={'Host': 'localhost'})
        html = response.get_data(as_text=True)

        self.assertEqual(200, response.status_code)
        self.assertIn('Approximate Location', html)
        self.assertNotIn('Approximate placement', html)
        self.assertNotIn('Approximate map placement', html)

    def test_api_request_contexts(self):
        cross_site = self.client.get(
            '/api/incidents/active',
            headers={
                'Host': 'louisiana911.com',
                'X-Forwarded-Proto': 'https',
                'Sec-Fetch-Site': 'cross-site',
                'Sec-Fetch-Mode': 'cors',
            },
        )
        direct_navigation = self.client.get(
            '/api/incidents/active',
            headers={
                'Host': 'louisiana911.com',
                'X-Forwarded-Proto': 'https',
                'Sec-Fetch-Site': 'none',
                'Sec-Fetch-Mode': 'navigate',
            },
        )
        headerless_client = self.client.get(
            '/api/incidents/active',
            headers={
                'Host': 'louisiana911.com',
                'X-Forwarded-Proto': 'https',
            },
        )

        self.assertEqual(403, cross_site.status_code)
        self.assertEqual(404, direct_navigation.status_code)
        self.assertEqual(404, headerless_client.status_code)

    def test_api_response_contract(self):
        headers = {
            'Host': 'louisiana911.com',
            'X-Forwarded-Proto': 'https',
            'Origin': 'https://louisiana911.com',
            'Sec-Fetch-Site': 'same-origin',
            'Sec-Fetch-Mode': 'cors',
        }
        active = self.client.get('/api/incidents/active', headers=headers)
        direct_history = self.client.get(
            '/api/incidents/history?date=2026-07-20',
            headers={**headers, 'X-Louisiana911-UI': 'history'},
        )

        non_navigation_shell = self.client.get('/', headers=headers)
        shell = self.client.get(
            '/',
            headers={
                **headers,
                'Sec-Fetch-Site': 'none',
                'Sec-Fetch-Mode': 'navigate',
                'Sec-Fetch-Dest': 'document',
            },
        )
        unbounded_history = self.client.get(
            '/api/incidents/history',
            headers={**headers, 'X-Louisiana911-UI': 'history'},
        )

        self.assertEqual(200, active.status_code)
        self.assertEqual('no-store, private', active.headers['Cache-Control'])
        self.assertEqual('noindex, nofollow, noarchive', active.headers['X-Robots-Tag'])
        self.assertEqual(404, direct_history.status_code)
        self.assertNotIn('l911_history_ui=', non_navigation_shell.headers.get('Set-Cookie', ''))
        self.assertIn('l911_history_ui=', shell.headers.get('Set-Cookie', ''))
        self.assertIn('Secure', shell.headers.get('Set-Cookie', ''))
        self.assertIn('HttpOnly', shell.headers.get('Set-Cookie', ''))
        self.assertIn('SameSite=Strict', shell.headers.get('Set-Cookie', ''))
        self.assertEqual(400, unbounded_history.status_code)

    def test_history_api_requires_ui_header_and_matching_browser_session(self):
        headers = {
            'Host': 'localhost',
            'Sec-Fetch-Site': 'same-origin',
            'Sec-Fetch-Mode': 'cors',
            'User-Agent': 'History UI Test Browser',
        }
        self.client.get(
            '/',
            headers={
                **headers,
                'Sec-Fetch-Site': 'none',
                'Sec-Fetch-Mode': 'navigate',
                'Sec-Fetch-Dest': 'document',
            },
        )

        missing_ui_header = self.client.get(
            '/api/incidents/history?date=2026-07-20',
            headers=headers,
        )
        permitted = self.client.get(
            '/api/incidents/history?date=2026-07-20',
            headers={**headers, 'X-Louisiana911-UI': 'history'},
        )
        counts = self.client.get(
            '/api/incidents/history_counts?month=2026-07',
            headers={**headers, 'X-Louisiana911-UI': 'history'},
        )
        changed_browser = self.client.get(
            '/api/incidents/history?date=2026-07-20',
            headers={
                **headers,
                'User-Agent': 'Different Browser',
                'X-Louisiana911-UI': 'history',
            },
        )

        self.assertEqual(404, missing_ui_header.status_code)
        self.assertEqual(404, changed_browser.status_code)
        self.assertEqual(200, permitted.status_code)
        self.assertEqual(200, counts.status_code)

    def test_history_ui_session_expires(self):
        headers = {
            'Host': 'localhost',
            'Sec-Fetch-Site': 'same-origin',
            'Sec-Fetch-Mode': 'cors',
            'User-Agent': 'Expiring History UI Test Browser',
            'X-Louisiana911-UI': 'history',
        }
        with patch('app.time.time', return_value=1_000_000):
            self.client.get(
                '/',
                headers={
                    **headers,
                    'Sec-Fetch-Site': 'none',
                    'Sec-Fetch-Mode': 'navigate',
                    'Sec-Fetch-Dest': 'document',
                },
            )

        with patch(
            'app.time.time',
            return_value=1_000_000 + app.HISTORY_UI_SESSION_MAX_AGE_SECONDS + 1,
        ):
            expired = self.client.get(
                '/api/incidents/history?date=2026-07-20',
                headers=headers,
            )
            renewed = self.client.get(
                '/api/incidents/history?date=2026-07-20',
                headers=headers,
            )

        self.assertEqual(404, expired.status_code)
        self.assertEqual(200, renewed.status_code)


if __name__ == '__main__':
    unittest.main()
