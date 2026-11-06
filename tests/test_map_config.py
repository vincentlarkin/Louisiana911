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
