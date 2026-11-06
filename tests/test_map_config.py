import json
import os
import re
import tempfile
import unittest
from unittest.mock import patch

os.environ.setdefault('LOUISIANA911_DB_PATH', os.path.join(tempfile.gettempdir(), f'map-config-tests-{os.getpid()}.db'))
import app


def page_config(html):
    match = re.search(r'<script type="application/json" id="map-config">(.*?)</script>', html)
    return json.loads(match.group(1))


class MapTileHostTests(unittest.TestCase):
    def tile_host(self, value):
        with patch.dict(os.environ, {'LOUISIANA911_MAP_TILE_HOST': value}), patch.object(app, 'log'):
            return app._map_tile_host()

    def test_accepts_only_a_bare_https_origin(self):
        self.assertEqual(self.tile_host('https://maps.louisiana911.com'), 'https://maps.louisiana911.com')
        self.assertEqual(self.tile_host(' https://Maps.Louisiana911.com/ '), 'https://maps.louisiana911.com')
        for bad in ['http://maps.louisiana911.com', 'maps.louisiana911.com', 'https://maps.louisiana911.com/tiles',
                    'https://maps.louisiana911.com?x=1', 'https://user:pw@maps.louisiana911.com',
                    'javascript:alert(1)', 'https://']:
            with self.subTest(value=bad):
                self.assertEqual(self.tile_host(bad), '')

    def test_unset_keeps_the_public_server(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop('LOUISIANA911_MAP_TILE_HOST', None)
            html = app._read_map_index_html()
        self.assertEqual(page_config(html)['tileHost'], '')
        self.assertIn('<link rel="preconnect" href="https://tiles.versatiles.org" crossorigin>', html)

    def test_configured_host_reaches_every_map_page_and_its_preconnect(self):
        with patch.dict(os.environ, {'LOUISIANA911_MAP_TILE_HOST': 'https://maps.louisiana911.com'}):
            html = app._read_map_index_html()
        self.assertEqual(page_config(html)['tileHost'], 'https://maps.louisiana911.com')
        self.assertIn('<link rel="preconnect" href="https://maps.louisiana911.com" crossorigin>', html)
        self.assertNotIn('<link rel="preconnect" href="https://tiles.versatiles.org"', html)


if __name__ == '__main__':
    unittest.main()


class AssetFingerprintTests(unittest.TestCase):
    def test_stylesheet_url_follows_its_content(self):
        with open(os.path.join(app.app.root_path, 'public', 'styles.css'), 'rb') as css:
            digest = app.hashlib.sha256(css.read()).hexdigest()[:12]
        html = app._read_map_index_html()
        self.assertIn(f'href="styles.css?v={digest}"', html)
        self.assertNotIn('styles.css?v=4.', html)

    def test_changed_stylesheet_gets_a_new_url(self):
        with tempfile.TemporaryDirectory() as root:
            os.makedirs(os.path.join(root, 'public'))
            css = os.path.join(root, 'public', 'styles.css')
            with open(css, 'w') as handle:
                handle.write('a{}')
            with patch.object(app.app, 'root_path', root):
                app._asset_fingerprints.clear()
                first = app._fingerprint_asset_urls('<link rel="stylesheet" href="styles.css?v=4.11.0" />')
                with open(css, 'w') as handle:
                    handle.write('b{}')
                os.utime(css, (1, 1))
                second = app._fingerprint_asset_urls('<link rel="stylesheet" href="styles.css?v=4.11.0" />')
            app._asset_fingerprints.clear()
        self.assertNotEqual(first, second)
        self.assertRegex(second, r'href="styles\.css\?v=[0-9a-f]{12}"')
