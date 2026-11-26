import dataclasses
import io
import os
import json
import tempfile
import unittest
import xml.etree.ElementTree as ET
from urllib.parse import urlsplit
from scripts.update_sitemap import indexable_pages, NAMESPACE
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
            '/coverage/lake-charles/': 'Lake Charles Police Calls',
            '/coverage/sulphur/': 'Sulphur Police Calls',
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
            '/coverage/lake-charles',
            '/coverage/sulphur',
        ):
            with self.subTest(path=path):
                response = self.client.get(path, headers={'Host': 'localhost'})
                self.assertEqual(301, response.status_code)
                self.assertEqual(f'{path}/', response.headers['Location'])

    def test_parish_landing_pages_serve_map_with_local_metadata_article_and_history_session(self):
        pages = {
            '/caddo911/': ('caddo', 'Caddo Parish'),
            '/coverage/baton-rouge/': ('batonrouge', 'Baton Rouge'),
            '/coverage/lafayette/': ('lafayette', 'Lafayette'),
            '/coverage/new-orleans/': ('neworleans', 'New Orleans'),
            '/coverage/lake-charles/': ('lakecharles', 'Lake Charles'),
            '/coverage/sulphur/': ('sulphur', 'Sulphur'),
        }
        for path, (source, place) in pages.items():
            with self.subTest(path=path):
                response = self.client.get(path, headers={
                    'Host': 'localhost', 'Sec-Fetch-Site': 'none',
                    'Sec-Fetch-Mode': 'navigate', 'Sec-Fetch-Dest': 'document',
                })
                html = response.get_data(as_text=True)
                self.assertEqual(200, response.status_code)
                self.assertIn('<div id="map"></div>', html)
                self.assertIn('<base href="/">', html)
                self.assertIn(f'id="coverage-intro" data-source="{source}"', html)
                self.assertEqual(1, html.count('rel="canonical"'))
                self.assertIn(f'href="https://louisiana911.com{path}"', html)
                self.assertIn(f'content="https://louisiana911.com{path}"', html)
                self.assertIn('"@type": "BreadcrumbList"', html)
                self.assertIn('<details class="coverage-details">', html)
                self.assertIn(place, html.split('class="coverage-article">', 1)[1])
                self.assertEqual(1, html.count('<h1'))
                self.assertIn('l911_history_ui=', response.headers.get('Set-Cookie', ''))
                self.assertEqual('no-store, private', response.headers['Cache-Control'])
        home = self.client.get('/').get_data(as_text=True)
        self.assertNotIn('id="coverage-intro"', home)

    def test_versioned_shell_assets_are_immutable(self):
        for path in (
            '/analytics.js?v=4.8.3',
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
            '/coverage/lake-charles/',
            '/coverage/sulphur/',
            '/reports/',
        ):
            with self.subTest(path=path):
                response = self.client.get(path, headers={'Host': 'localhost'})
                self.assertEqual(200, response.status_code)
                html = response.get_data(as_text=True)
                self.assertEqual(1, html.count('/analytics.js?v=4.8.3'))
                self.assertIn('<script defer src="/analytics.js?v=4.8.3"></script>', html)
                self.assertNotIn('googletagmanager.com/gtag/js', html)

    def test_sitemap_contains_only_complete_canonical_indexable_pages(self):
        response = self.client.get('/sitemap.xml', headers={'Host': 'localhost'})
        self.assertEqual(200, response.status_code)
        root = ET.fromstring(response.data)
        urls = [entry.find(f'{{{NAMESPACE}}}loc').text for entry in root]
        self.assertEqual(sorted(indexable_pages()), sorted(urls))
        self.assertEqual(len(urls), len(set(urls)))
        for url in urls:
            with self.subTest(url=url):
                page = self.client.get(urlsplit(url).path, headers={'Host': 'localhost'})
                self.assertEqual(200, page.status_code)
                html = page.get_data(as_text=True)
                self.assertIn(f'<link rel="canonical" href="{url}">', html)
                self.assertNotIn('noindex', html)
                self.assertNotIn('/incident/', url)
        robots = self.client.get('/robots.txt', headers={'Host': 'localhost'}).get_data(as_text=True)
        self.assertIn('Sitemap: https://louisiana911.com/sitemap.xml', robots)
        self.assertIn('Disallow: /api/', robots)

    def test_analytics_bundle_includes_key_engagement_events(self):
        response = self.client.get('/analytics.js?v=4.8.3', headers={'Host': 'localhost'})
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
        self.assertIn('<title>Mass Casualty Incident in Shreveport | Louisiana911</title>', html)
        self.assertIn('<meta property="og:title" content="Mass Casualty Incident in Shreveport">', html)
        self.assertIn('<meta name="twitter:title" content="Mass Casualty Incident in Shreveport">', html)
        self.assertIn(
            '<meta property="og:description" content="Very active call near 100 Test St @ Sample Ave, '
            'Shreveport. Reported Aug 29 at 2:30 PM. Shreveport Fire, 8 units.">',
            html,
        )
        self.assertNotIn('MASS CASUALTY INCIDENT |', html)
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
        self.assertIn('content="Earlier call near 100 Test St @ Sample Ave, Shreveport.', historical_html)
        self.assertNotIn('Very active call', historical_html)
        self.assertIn('"is_active":0', historical_html)

    def _insert_share_incident(self, share_id, **overrides):
        row = {
            'hash': share_id, 'agency': 'SPD', 'time': '1118', 'units': 2, 'description': 'MEDICAL EMERGENCY',
            'street': 'AMHERST', 'cross_streets': 'BURKE ST & QUILEN BLVD', 'municipality': 'SHV',
            'source': 'caddo', 'latitude': 32.4655, 'longitude': -93.7377,
            'first_seen': '2026-11-26T17:20:00+00:00', 'last_seen': '2026-11-26T17:25:00+00:00', 'is_active': 1,
        }
        row.update(overrides)
        conn = app.db_connect()
        try:
            conn.execute(
                f"INSERT INTO incidents ({', '.join(row)}) VALUES ({', '.join('?' for _ in row)})",
                tuple(row.values()),
            )
            conn.commit()
        finally:
            conn.close()
        self.addCleanup(self._delete_incident, share_id)

    def test_share_page_points_previews_at_the_call_card(self):
        share_id = '1123456789abcdef0123456789abcdef'
        self._insert_share_incident(share_id)
        html = self.client.get(f'/incident/{share_id}', headers={'Host': 'localhost'}).get_data(as_text=True)
        card_url = app._share_image_url(share_id, app._share_card(app._find_incident_by_share_id(share_id)))
        self.assertRegex(card_url, rf'^https://louisiana911\.com/og/incident/{share_id}\.jpg\?v=[0-9a-f]{{12}}$')
        self.assertIn(f'<meta property="og:image" content="{card_url}">', html)
        self.assertIn(f'<meta name="twitter:image" content="{card_url}">', html)
        self.assertIn('<meta property="og:image:type" content="image/jpeg">', html)
        self.assertIn('<meta property="og:image:width" content="1200">', html)
        self.assertIn('<meta property="og:image:height" content="630">', html)
        self.assertIn('content="Map of Medical Emergency near Amherst @ Burke St &amp; Quilen Blvd, Shreveport"', html)
        self.assertNotIn('https://louisiana911.com/og.png', html)

    def test_call_card_renders_once_then_comes_from_the_disk_cache(self):
        from PIL import Image
        share_id = '2123456789abcdef0123456789abcdef'
        self._insert_share_incident(share_id)
        tiles = []

        def fake_tile(z, x, y):
            tiles.append((z, x, y))
            return Image.new('RGB', (512, 512), (40, 44, 52))

        with tempfile.TemporaryDirectory() as cache_dir, \
                patch.dict(os.environ, {'LOUISIANA911_SHARE_IMAGE_CACHE_DIR': cache_dir}), \
                patch.object(app, '_fetch_share_map_tile', side_effect=fake_tile):
            signature = app._share_card_signature(app._share_card(app._find_incident_by_share_id(share_id)))
            first = self.client.get(f'/og/incident/{share_id}.jpg?v={signature}', headers={'Host': 'localhost'})
            self.assertEqual(200, first.status_code)
            self.assertEqual('image/jpeg', first.mimetype)
            self.assertEqual('public, max-age=604800, immutable', first.headers['Cache-Control'])
            self.assertEqual('noindex', first.headers['X-Robots-Tag'])
            with Image.open(io.BytesIO(first.data)) as card:
                self.assertEqual((1200, 630), card.size)
            self.assertTrue(tiles)
            self.assertTrue(all(z == 15 for z, _, _ in tiles))
            self.assertEqual([f'{share_id}-{signature}.jpg'], os.listdir(cache_dir))

            tiles.clear()
            again = self.client.get(f'/og/incident/{share_id}.jpg?v={signature}', headers={'Host': 'localhost'})
            self.assertEqual(first.data, again.data)
            self.assertEqual([], tiles)

            # An old ?v= still gets the current card, just not cached for good.
            stale = self.client.get(f'/og/incident/{share_id}.jpg?v=000000000000', headers={'Host': 'localhost'})
            self.assertEqual('public, max-age=3600', stale.headers['Cache-Control'])

    def test_call_card_without_map_tiles_is_drawn_but_cached_briefly(self):
        share_id = '3123456789abcdef0123456789abcdef'
        self._insert_share_incident(share_id)
        with tempfile.TemporaryDirectory() as cache_dir, \
                patch.dict(os.environ, {'LOUISIANA911_SHARE_IMAGE_CACHE_DIR': cache_dir}), \
                patch.object(app, '_fetch_share_map_tile', return_value=None):
            response = self.client.get(f'/og/incident/{share_id}.jpg', headers={'Host': 'localhost'})
            self.assertEqual(200, response.status_code)
            self.assertEqual('public, max-age=300', response.headers['Cache-Control'])
            self.assertEqual([], os.listdir(cache_dir))

        # No coordinates at all: the parish-outline card is the finished card.
        share_id = '4123456789abcdef0123456789abcdef'
        self._insert_share_incident(share_id, latitude=None, longitude=None, street='', cross_streets='')
        with tempfile.TemporaryDirectory() as cache_dir, \
                patch.dict(os.environ, {'LOUISIANA911_SHARE_IMAGE_CACHE_DIR': cache_dir}), \
                patch.object(app, '_fetch_share_map_tile', side_effect=AssertionError('no tiles needed')):
            response = self.client.get(f'/og/incident/{share_id}.jpg', headers={'Host': 'localhost'})
            self.assertEqual(200, response.status_code)
            self.assertEqual(1, len(os.listdir(cache_dir)))

        missing = self.client.get(f'/og/incident/{"e" * 32}.jpg', headers={'Host': 'localhost'})
        self.assertEqual(404, missing.status_code)

    def test_every_page_points_previews_at_its_card(self):
        from scripts.update_preview_tags import PAGE_FILES, update_page
        public = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'public')
        self.assertEqual(sorted(name for name in os.listdir(public) if name.endswith('.html')), sorted(PAGE_FILES))
        for name, slug in PAGE_FILES.items():
            with self.subTest(name):
                with open(os.path.join(public, name), encoding='utf-8') as handle:
                    html = handle.read()
                self.assertEqual(update_page(html, slug, app), html,
                                 'Preview tags are out of date: run python scripts/update_preview_tags.py')
                url = app._page_image_url(slug)
                self.assertRegex(url, rf'^https://louisiana911\.com/og/page/{slug}\.jpg\?v=[0-9a-f]{{12}}$')
                self.assertIn(f'<meta property="og:image" content="{url}">', html)
                self.assertIn(f'<meta name="twitter:image" content="{url}">', html)
                self.assertIn('<meta name="twitter:card" content="summary_large_image">', html)
                self.assertNotIn('og.png', html)

        # Coverage pages carry their own card into the map shell.
        html = self.client.get('/coverage/sulphur/', headers={'Host': 'localhost'}).get_data(as_text=True)
        self.assertIn(f'<meta property="og:image" content="{app._page_image_url("sulphur")}">', html)
        self.assertIn('<meta property="og:image:alt" content="Map of Calcasieu Parish, including Sulphur">', html)
        self.assertNotIn(app._page_image_url('louisiana'), html)

    def test_page_card_renders_once_then_comes_from_the_disk_cache(self):
        from PIL import Image
        tiles = []

        def fake_tile(z, x, y, style='dark_all'):
            tiles.append((style, z))
            return Image.new('RGB', (512, 512), (40, 44, 52))

        card, _ = app.PAGE_CARDS['caddo']
        signature = app._page_card_signature(card)
        with tempfile.TemporaryDirectory() as cache_dir, \
                patch.dict(os.environ, {'LOUISIANA911_SHARE_IMAGE_CACHE_DIR': cache_dir}), \
                patch.object(app, '_fetch_share_map_tile', side_effect=fake_tile):
            first = self.client.get(f'/og/page/caddo.jpg?v={signature}', headers={'Host': 'localhost'})
            self.assertEqual(200, first.status_code)
            self.assertEqual('image/jpeg', first.mimetype)
            self.assertEqual('public, max-age=604800, immutable', first.headers['Cache-Control'])
            with Image.open(io.BytesIO(first.data)) as image:
                self.assertEqual((1200, 630), image.size)
            # The page cards label the towns themselves.
            self.assertTrue(tiles)
            self.assertEqual({'dark_nolabels'}, {style for style, _ in tiles})
            self.assertEqual([f'page-caddo-{signature}.jpg'], os.listdir(cache_dir))

            tiles.clear()
            again = self.client.get(f'/og/page/caddo.jpg?v={signature}', headers={'Host': 'localhost'})
            self.assertEqual(first.data, again.data)
            self.assertEqual([], tiles)

        with tempfile.TemporaryDirectory() as cache_dir, \
                patch.dict(os.environ, {'LOUISIANA911_SHARE_IMAGE_CACHE_DIR': cache_dir}), \
                patch.object(app, '_fetch_share_map_tile', return_value=None):
            outline_only = self.client.get('/og/page/louisiana.jpg', headers={'Host': 'localhost'})
            self.assertEqual(200, outline_only.status_code)
            self.assertEqual('public, max-age=300', outline_only.headers['Cache-Control'])
            self.assertEqual([], os.listdir(cache_dir))

        self.assertEqual(404, self.client.get('/og/page/texas.jpg', headers={'Host': 'localhost'}).status_code)

    def test_page_card_urls_change_when_a_card_or_outline_changes(self):
        card, _ = app.PAGE_CARDS['lake-charles']
        before = app._page_card_signature(card)
        self.assertNotEqual(before, app._page_card_signature(dataclasses.replace(card, title='Lake Charles calls')))
        with patch.object(app.share_image, 'outline_digest', return_value='0' * 12):
            self.assertNotEqual(before, app._page_card_signature(card))
        with patch.object(app, 'SHARE_CARD_VERSION', 'next'):
            self.assertNotEqual(before, app._page_card_signature(card))

    def test_call_card_words_and_colors(self):
        card = app._share_card({
            'source': 'batonrouge', 'agency': 'LAW', 'time': '1731', 'units': 1, 'is_active': 0,
            'description': 'TRAFFIC CRASH IN ROADWAY', 'street': '10400 - 10499 S MALL DR',
            'cross_streets': 'ANDREA DR / SIEGEN LN', 'municipality': 'Baton Rouge',
            'latitude': 30.388, 'longitude': -91.075, 'geocode_quality': 'approximate-block',
            'first_seen': '2026-10-09T22:31:00+00:00',
        })
        self.assertEqual('Traffic Crash in Roadway', card.title)
        self.assertEqual(('Earlier call', 'Moderate'), (card.status, card.severity))
        self.assertEqual((255, 184, 48), card.severity_color)
        self.assertEqual('10400 - 10499 S Mall Dr @ Andrea Dr / Siegen Ln, Baton Rouge', card.place)
        self.assertEqual('Oct 9, 5:31 PM · Law enforcement · 1 unit', card.details)
        self.assertTrue(card.approximate)
        self.assertFalse(card.active)
        # Coordinates outside the map's Louisiana bounds are not drawn.
        self.assertIsNone(app._share_card({'source': 'caddo', 'latitude': 0, 'longitude': 0}).latitude)

    def test_plain_case_matches_the_map_cases(self):
        cases_path = os.path.join(os.path.dirname(__file__), 'plain_text_cases.json')
        with open(cases_path, encoding='utf-8') as handle:
            for case in json.load(handle):
                with self.subTest(raw=case['raw']):
                    self.assertEqual(case['plain'], app._plain_case(case['raw']))

    def test_share_preview_text_reads_plainly_for_each_feed(self):
        title, description = app._share_preview_text({
            'source': 'caddo', 'agency': 'SPD', 'time': '1118', 'units': 1, 'is_active': 1,
            'description': 'LOUD MUSIC/NOISE', 'street': 'E 73RD RD', 'cross_streets': 'THORNHILL AV & LINE AV',
            'municipality': 'SHV', 'first_seen': '2026-11-26T17:20:00+00:00',
        })
        self.assertEqual('Loud Music/Noise in Shreveport', title)
        self.assertEqual('Active call near E 73rd Rd @ Thornhill Av & Line Av, Shreveport. '
                         'Reported Nov 26 at 11:18 AM. Shreveport Police, 1 unit.', description)

        title, description = app._share_preview_text({
            'source': 'lakecharles', 'agency': 'LCPD', 'units': 0, 'is_active': 0,
            'description': 'HANG UP CALL', 'street': '1700 BLK GIEFFERS ST, LAKE CHARLES',
            'municipality': 'Lake Charles', 'first_seen': '2026-11-25T21:10:00+00:00',
        })
        self.assertEqual('Hang Up Call in Lake Charles', title)
        self.assertEqual('Closed call near 1700 Blk Gieffers St, Lake Charles. '
                         'Reported Nov 25 at 3:10 PM, posted a day later. Lake Charles Police.', description)

        title, description = app._share_preview_text({
            'source': 'caddo', 'agency': 'CSO', 'units': 0, 'is_active': 0,
            'description': 'THEFT', 'street': '', 'cross_streets': '', 'municipality': 'CADD',
        })
        self.assertEqual('Theft in Caddo Parish', title)
        self.assertEqual('Earlier call in Caddo Parish. Caddo Sheriff.', description)

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
        # The live list is shared and edge-cached briefly; everything else stays private.
        self.assertEqual('public, max-age=0, s-maxage=15', active.headers['Cache-Control'])
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
