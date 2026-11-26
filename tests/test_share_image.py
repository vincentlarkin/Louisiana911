import io
import unittest

from PIL import Image

import share_image


def solid_tile(z, x, y):
    return Image.new('RGB', (512, 512), (30, 32, 38))


def card(**overrides):
    values = dict(
        title='Medical Emergency', status='Active call', status_color=(255, 59, 59),
        severity='Medical', severity_color=(255, 59, 59),
        place='Amherst @ Burke St & Quilen Blvd, Shreveport',
        details='Nov 26, 11:18 AM · Shreveport Fire · 2 units', source='caddo',
        latitude=32.4655, longitude=-93.7377, approximate=False, active=True,
    )
    values.update(overrides)
    return share_image.ShareCard(**values)


class ShareImageTests(unittest.TestCase):
    def assert_card(self, data):
        with Image.open(io.BytesIO(data)) as image:
            self.assertEqual('JPEG', image.format)
            self.assertEqual((1200, 630), image.size)
        # WhatsApp drops preview images much over 300 KB.
        self.assertLess(len(data), 300_000)

    def test_every_card_shape_draws(self):
        cases = {
            'exact pin': (card(), solid_tile, True),
            'approximate circle': (card(approximate=True, active=False), solid_tile, True),
            'long words wrap and clip': (card(title='Hit and Run Rescue Squad Needed ' * 4,
                                              place='717 E Pont des Mouton Rd @ Pont des Mouton Dr, Lafayette ' * 3), solid_tile, True),
            'tiles failed: parish outline': (card(), lambda z, x, y: None, False),
            'no coordinates': (card(latitude=None, longitude=None, place=''), solid_tile, False),
            'no outline file entry: site icon': (card(source='lakecharles', latitude=None, longitude=None), solid_tile, False),
        }
        for name, (shape, fetch, drew_map) in cases.items():
            with self.subTest(name):
                data, used_map = share_image.render_share_card(shape, fetch)
                self.assert_card(data)
                self.assertEqual(drew_map, used_map)

    def test_one_missing_tile_drops_the_whole_map(self):
        calls = []

        def flaky(z, x, y):
            calls.append((x, y))
            return None if len(calls) == 2 else solid_tile(z, x, y)

        _, used_map = share_image.render_share_card(card(), flaky)
        self.assertFalse(used_map)
        self.assertGreater(len(calls), 1)

    def test_long_title_is_clipped_with_an_ellipsis(self):
        font, lines = share_image._fit_title('Word ' * 60)
        self.assertLessEqual(len(lines), 3)
        self.assertTrue(lines[-1].endswith('…'))
        self.assertTrue(all(font.getlength(line) <= share_image.TEXT_WIDTH for line in lines))


if __name__ == '__main__':
    unittest.main()
