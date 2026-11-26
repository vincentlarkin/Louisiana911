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


STATE_PARISHES = ('22017', '22033', '22055', '22071', '22019')
LOUISIANA = (28.93, -94.05, 33.02, -88.82)


def page_card(**overrides):
    values = dict(
        eyebrow='Live 911 calls', eyebrow_color=(255, 59, 59), title='Caddo 911',
        summary='Police, fire, EMS and sheriff calls in Shreveport and Caddo Parish, checked about once a minute.',
        details='Shreveport Police · Shreveport Fire · Caddo Sheriff', parish_ids=('22017',),
        places=(('Shreveport', 32.5093, -93.7501, 'right'),),
    )
    values.update(overrides)
    return share_image.PageCard(**values)


def gold_pixels(image):
    gold = share_image.GOLD
    return sum(1 for pixel in image.convert('RGB').get_flattened_data()
               if all(abs(channel - target) < 40 for channel, target in zip(pixel, gold)))


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
            'Calcasieu outline': (card(source='lakecharles', latitude=None, longitude=None), solid_tile, False),
            'no outline file entry: site icon': (card(source='unknown', latitude=None, longitude=None), solid_tile, False),
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

    def test_calls_without_a_location_get_their_parish_outline(self):
        for source in ('caddo', 'batonrouge', 'lafayette', 'neworleans', 'lakecharles', 'sulphur'):
            with self.subTest(source):
                canvas, _ = share_image._draw_fallback(card(source=source, latitude=None, longitude=None))
                self.assertTrue(gold_pixels(canvas), 'expected the gold parish border')

    def test_every_page_card_draws_with_and_without_tiles(self):
        state = page_card(parish_ids=STATE_PARISHES, bounds=LOUISIANA, places=(
            ('Shreveport', 32.5093, -93.7501, 'right'), ('Lake Charles', 30.2266, -93.2174, 'below'),
            ('Lafayette', 30.2241, -92.0198, 'above'), ('New Orleans', 29.9511, -90.0715, 'left')))
        cases = {
            'one parish': page_card(),
            'statewide': state,
            'long words clip': page_card(title='Coverage by city and parish ' * 3, summary='Where each feed comes from. ' * 12),
            'Orleans (several rings)': page_card(parish_ids=('22071',)),
            'Calcasieu': page_card(parish_ids=('22019',)),
            'unknown parish: text only': page_card(parish_ids=('99999',)),
        }
        for name, shape in cases.items():
            for fetch, drew_map in ((solid_tile, bool(share_image._area_rings(shape.parish_ids))), (lambda z, x, y: None, False)):
                with self.subTest(name, tiles=drew_map):
                    data, used_map = share_image.render_page_card(shape, fetch)
                    self.assert_card(data)
                    self.assertEqual(drew_map, used_map)

    def test_page_card_map_fits_the_outline_inside_the_map_area(self):
        rings = share_image._area_rings(('22017',))
        _, project, _ = share_image._draw_area_map(rings, solid_tile)
        xs, ys = zip(*(project(lat, lon) for ring in rings for lon, lat in ring))
        # Clear of the text panel's fade and the card edges.
        self.assertGreaterEqual(min(xs), share_image.PAGE_FADE[1] - 1)
        self.assertLessEqual(max(xs), share_image.WIDTH - 20)
        self.assertGreaterEqual(min(ys), 20)
        self.assertLessEqual(max(ys), share_image.HEIGHT - 20)

    def test_outline_digest_covers_the_state_border_only_on_statewide_cards(self):
        parish = page_card()
        state = page_card(bounds=LOUISIANA)
        self.assertNotEqual(share_image.outline_digest(parish), share_image.outline_digest(state))
        self.assertEqual(share_image.outline_digest(parish), share_image.outline_digest(page_card(title='Other')))

    def test_long_title_is_clipped_with_an_ellipsis(self):
        font, lines = share_image._fit_title('Word ' * 60)
        self.assertLessEqual(len(lines), 3)
        self.assertTrue(lines[-1].endswith('…'))
        self.assertTrue(all(font.getlength(line) <= share_image.TEXT_WIDTH for line in lines))


if __name__ == '__main__':
    unittest.main()
