"""Draw the 1200x630 link-preview card for a shared incident.

The card puts the call type, street, time and agency on the left and a
street map with a pin on the right. Map tiles come from a caller-supplied
fetcher so this module does no network I/O and tests can run offline. When
the incident has no coordinates or any tile fails, the map side falls back
to the parish outline (or the Louisiana911 icon) and the caller is told so
it can cache that version briefly.
"""
from __future__ import annotations

import functools
import io
import json
import math
import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Callable

from PIL import Image, ImageDraw, ImageFilter, ImageFont

WIDTH, HEIGHT = 1200, 630
PAD = 64
TEXT_WIDTH = 560
TILE_SIZE = 512  # CARTO @2x tiles
MAP_ZOOM = 15
PIN_X, PIN_Y = 905, 330  # where the incident lands on the card

BG = (10, 11, 16)
TEXT = (241, 242, 245)
TEXT_DIM = (150, 154, 167)
GOLD = (217, 166, 70)

ROOT = os.path.dirname(os.path.abspath(__file__))
FONT_PATH = os.path.join(ROOT, 'public', 'fonts', 'space-grotesk-5.3.0', 'latin.woff2')
ICON_PATH = os.path.join(ROOT, 'public', 'images', 'louisiana911-icon-192.png')
OUTLINES_PATH = os.path.join(ROOT, 'public', 'parish-outlines.json')
SOURCE_PARISH_IDS = {'caddo': '22017', 'batonrouge': '22033', 'lafayette': '22055', 'neworleans': '22071'}
MAP_ATTRIBUTION = '© OpenStreetMap contributors © CARTO'


@dataclass(frozen=True)
class ShareCard:
    title: str                 # "Medical Emergency"
    status: str                # "Active call"
    status_color: tuple        # RGB
    severity: str              # "Serious"
    severity_color: tuple      # RGB
    place: str                 # "Amherst @ Burke St & Quilen Blvd, Shreveport" or ""
    details: str               # "Nov 26, 11:18 AM · Shreveport Fire · 2 units"
    source: str                # normalized feed name, for the outline fallback
    latitude: float | None = None
    longitude: float | None = None
    approximate: bool = False
    active: bool = False


TileFetcher = Callable[[int, int, int], 'Image.Image | None']


@functools.lru_cache(maxsize=32)
def _font(weight: int, size: int) -> ImageFont.FreeTypeFont:
    font = ImageFont.truetype(FONT_PATH, size)
    font.set_variation_by_axes([weight])
    return font


def _wrap(text: str, font: ImageFont.FreeTypeFont, width: int) -> list[str]:
    lines: list[str] = []
    line = ''
    for word in text.split():
        candidate = f'{line} {word}'.strip()
        if not line or font.getlength(candidate) <= width:
            line = candidate
        else:
            lines.append(line)
            line = word
    if line:
        lines.append(line)
    return lines


def _clip_lines(lines: list[str], font: ImageFont.FreeTypeFont, width: int, limit: int) -> list[str]:
    if len(lines) <= limit:
        return lines
    kept = lines[:limit]
    last = kept[-1]
    while last and font.getlength(f'{last}…') > width:
        last = last[:-1].rstrip()
    kept[-1] = f'{last}…'
    return kept


def _fit_title(text: str) -> tuple[ImageFont.FreeTypeFont, list[str]]:
    for size in (66, 58, 50, 44):
        font = _font(700, size)
        lines = _wrap(text, font, TEXT_WIDTH)
        if len(lines) <= 2 or (size <= 50 and len(lines) <= 3):
            return font, lines
    font = _font(700, 44)
    return font, _clip_lines(_wrap(text, font, TEXT_WIDTH), font, TEXT_WIDTH, 3)


def _world_pixel(lat: float, lon: float, zoom: int) -> tuple[float, float]:
    scale = TILE_SIZE * (2 ** zoom)
    lat = max(min(lat, 85.0511), -85.0511)
    x = (lon + 180.0) / 360.0 * scale
    sin = math.sin(math.radians(lat))
    y = (0.5 - math.log((1 + sin) / (1 - sin)) / (4 * math.pi)) * scale
    return x, y


def _draw_map(card: ShareCard, fetch_tile: TileFetcher) -> Image.Image | None:
    """Street map with the incident at (PIN_X, PIN_Y), or None if any tile is missing."""
    if card.latitude is None or card.longitude is None:
        return None
    world_x, world_y = _world_pixel(card.latitude, card.longitude, MAP_ZOOM)
    origin_x, origin_y = world_x - PIN_X, world_y - PIN_Y
    left = int(origin_x + 520) // TILE_SIZE  # the text panel hides everything left of ~600 px
    right = int(origin_x + WIDTH - 1) // TILE_SIZE
    top = int(origin_y) // TILE_SIZE
    bottom = int(origin_y + HEIGHT - 1) // TILE_SIZE
    limit = 2 ** MAP_ZOOM
    wanted = [(x, y) for x in range(left, right + 1) for y in range(top, bottom + 1)]
    if any(not (0 <= x < limit and 0 <= y < limit) for x, y in wanted):
        return None
    with ThreadPoolExecutor(max_workers=min(6, len(wanted))) as pool:
        tiles = list(pool.map(lambda xy: fetch_tile(MAP_ZOOM, xy[0], xy[1]), wanted))
    if any(tile is None for tile in tiles):
        return None
    canvas = Image.new('RGB', (WIDTH, HEIGHT), BG)
    for (x, y), tile in zip(wanted, tiles):
        if tile.size != (TILE_SIZE, TILE_SIZE):
            tile = tile.resize((TILE_SIZE, TILE_SIZE), Image.LANCZOS)
        canvas.paste(tile.convert('RGB'), (round(x * TILE_SIZE - origin_x), round(y * TILE_SIZE - origin_y)))
    return canvas


@functools.lru_cache(maxsize=1)
def _parish_outlines() -> dict:
    try:
        with open(OUTLINES_PATH, encoding='utf-8') as handle:
            features = json.load(handle).get('features', [])
    except (OSError, ValueError):
        return {}
    outlines = {}
    for feature in features:
        geometry = feature.get('geometry') or {}
        polygons = geometry.get('coordinates') or []
        if geometry.get('type') == 'Polygon':
            polygons = [polygons]
        outlines[str(feature.get('id'))] = [polygon[0] for polygon in polygons if polygon]
    return outlines


def _draw_fallback(card: ShareCard) -> tuple[Image.Image, tuple[float, float] | None]:
    """The map side without tiles: the parish outline with a pin, or the site icon."""
    canvas = Image.new('RGB', (WIDTH, HEIGHT), BG)
    rings = _parish_outlines().get(SOURCE_PARISH_IDS.get(card.source, ''), [])
    box_left, box_top, box_right, box_bottom = 680, 70, 1140, 560
    if rings:
        points = [_world_pixel(lat, lon, 0) for ring in rings for lon, lat in ring]
        min_x, max_x = min(p[0] for p in points), max(p[0] for p in points)
        min_y, max_y = min(p[1] for p in points), max(p[1] for p in points)
        scale = min((box_right - box_left) / (max_x - min_x), (box_bottom - box_top) / (max_y - min_y))
        offset_x = box_left + ((box_right - box_left) - (max_x - min_x) * scale) / 2
        offset_y = box_top + ((box_bottom - box_top) - (max_y - min_y) * scale) / 2

        def project(lat, lon):
            x, y = _world_pixel(lat, lon, 0)
            return offset_x + (x - min_x) * scale, offset_y + (y - min_y) * scale

        layer = Image.new('RGBA', (WIDTH * 2, HEIGHT * 2), (0, 0, 0, 0))
        draw = ImageDraw.Draw(layer)
        for ring in rings:
            shape = [tuple(v * 2 for v in project(lat, lon)) for lon, lat in ring]
            draw.polygon(shape, fill=(169, 196, 255, 26))
            draw.line(shape + [shape[0]], fill=GOLD + (240,), width=5, joint='curve')
        canvas.paste(layer.resize((WIDTH, HEIGHT), Image.LANCZOS), (0, 0), layer.resize((WIDTH, HEIGHT), Image.LANCZOS))
        if card.latitude is not None and card.longitude is not None:
            pin = project(card.latitude, card.longitude)
            if box_left - 20 <= pin[0] <= box_right + 20 and box_top - 20 <= pin[1] <= box_bottom + 20:
                return canvas, pin
        return canvas, None
    try:
        icon = Image.open(ICON_PATH).convert('RGBA').resize((300, 300), Image.LANCZOS)
        faded = icon.copy()
        faded.putalpha(icon.getchannel('A').point(lambda a: a * 55 // 100))
        canvas.paste(faded, (760, 165), faded)
    except OSError:
        pass
    return canvas, None


def _draw_pin(canvas: Image.Image, x: float, y: float, card: ShareCard) -> None:
    """A teardrop pin whose tip sits on (x, y); a soft circle for approximate spots."""
    factor = 4
    size = 160
    layer = Image.new('RGBA', (size * factor, size * factor), (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    cx, cy = size * factor / 2, size * factor * 0.72
    color = tuple(card.severity_color)
    f = factor
    if card.approximate:
        draw.ellipse((cx - 46 * f, cy - 46 * f, cx + 46 * f, cy + 46 * f), fill=color + (60,), outline=color + (230,), width=3 * f)
        draw.ellipse((cx - 10 * f, cy - 10 * f, cx + 10 * f, cy + 10 * f), fill=color + (255,), outline=(255, 255, 255, 255), width=3 * f)
    else:
        if card.active:
            draw.ellipse((cx - 30 * f, cy - 12 * f, cx + 30 * f, cy + 12 * f), outline=color + (150,), width=3 * f)
        head_y = cy - 50 * f
        radius = 27 * f
        draw.polygon([(cx - radius * 0.82, head_y + radius * 0.58), (cx + radius * 0.82, head_y + radius * 0.58), (cx, cy)],
                     fill=(255, 255, 255, 255))
        draw.ellipse((cx - radius - 4 * f, head_y - radius - 4 * f, cx + radius + 4 * f, head_y + radius + 4 * f), fill=(255, 255, 255, 255))
        inner = radius - 1 * f
        draw.polygon([(cx - inner * 0.8, head_y + inner * 0.6), (cx + inner * 0.8, head_y + inner * 0.6), (cx, cy - 6 * f)], fill=color + (255,))
        draw.ellipse((cx - inner, head_y - inner, cx + inner, head_y + inner), fill=color + (255,))
        draw.ellipse((cx - 10 * f, head_y - 10 * f, cx + 10 * f, head_y + 10 * f), fill=(255, 255, 255, 255))
    pin = layer.resize((size, size), Image.LANCZOS)
    shadow = Image.new('RGBA', pin.size, (0, 0, 0, 0))
    shadow.putalpha(pin.getchannel('A').point(lambda a: a * 120 // 255))
    shadow = shadow.filter(ImageFilter.GaussianBlur(6))
    left, top = round(x - size / 2), round(y - size * 0.72)
    canvas.paste(shadow, (left + 2, top + 5), shadow)
    canvas.paste(pin, (left, top), pin)


def _draw_text_panel(canvas: Image.Image, card: ShareCard) -> None:
    # Solid behind the text, fading into the map.
    fade = Image.new('L', (WIDTH, 1), 0)
    for x in range(WIDTH):
        if x < 580:
            alpha = 255
        elif x < 860:
            t = (x - 580) / 280
            alpha = round(255 * (1 - t * t * (3 - 2 * t)))
        else:
            alpha = 0
        fade.putpixel((x, 0), alpha)
    canvas.paste(Image.new('RGB', (WIDTH, HEIGHT), BG), (0, 0), fade.resize((WIDTH, HEIGHT)))

    draw = ImageDraw.Draw(canvas)
    y = PAD - 6

    # Status line: colored dot, status words, severity pill.
    status_font = _font(600, 30)
    dot = 14
    draw.ellipse((PAD, y + 11, PAD + dot, y + 11 + dot), fill=card.status_color)
    x = PAD + dot + 12
    draw.text((x, y), card.status, font=status_font, fill=card.status_color)
    x += status_font.getlength(card.status) + 18
    if card.severity:
        pill_font = _font(600, 24)
        pill_w = pill_font.getlength(card.severity) + 30
        tint = tuple(round(c * 0.22 + b * 0.78) for c, b in zip(card.severity_color, BG))
        draw.rounded_rectangle((x, y + 1, x + pill_w, y + 39), radius=20, fill=tint,
                               outline=tuple(round(c * 0.55 + b * 0.45) for c, b in zip(card.severity_color, BG)), width=2)
        draw.text((x + 15, y + 6), card.severity, font=pill_font, fill=card.severity_color)
    y += 66

    title_font, title_lines = _fit_title(card.title)
    title_step = round(title_font.size * 1.08)
    for line in title_lines:
        draw.text((PAD, y), line, font=title_font, fill=TEXT)
        y += title_step
    y += 18

    place_font = _font(500, 32)
    place = card.place or 'Location not listed'
    place_lines = _clip_lines(_wrap(place, place_font, TEXT_WIDTH), place_font, TEXT_WIDTH, 3 if len(title_lines) < 3 else 2)
    for line in place_lines:
        draw.text((PAD, y), line, font=place_font, fill=TEXT if card.place else TEXT_DIM)
        y += 42
    if card.approximate and card.place:
        draw.text((PAD, y + 2), 'Approximate location', font=_font(500, 24), fill=TEXT_DIM)
        y += 34
    y += 10

    detail_font = _font(400, 28)
    for line in _clip_lines(_wrap(card.details, detail_font, TEXT_WIDTH), detail_font, TEXT_WIDTH, 2):
        draw.text((PAD, y), line, font=detail_font, fill=TEXT_DIM)
        y += 38

    # Footer: the app icon and the site name.
    footer_y = HEIGHT - PAD - 46
    try:
        icon = Image.open(ICON_PATH).convert('RGBA').resize((46, 46), Image.LANCZOS)
        mask = Image.new('L', (46 * 4, 46 * 4), 0)
        ImageDraw.Draw(mask).rounded_rectangle((0, 0, 46 * 4 - 1, 46 * 4 - 1), radius=44, fill=255)
        mask = mask.resize((46, 46), Image.LANCZOS)
        canvas.paste(icon, (PAD, footer_y), Image.composite(icon.getchannel('A'), Image.new('L', (46, 46), 0), mask))
    except OSError:
        pass
    draw.text((PAD + 60, footer_y + 5), 'louisiana911.com', font=_font(600, 30), fill=TEXT)


def render_share_card(card: ShareCard, fetch_tile: TileFetcher) -> tuple[bytes, bool]:
    """Return (JPEG bytes, whether the street map was drawn)."""
    canvas = _draw_map(card, fetch_tile)
    used_map = canvas is not None
    pin = (PIN_X, PIN_Y) if used_map else None
    if canvas is None:
        canvas, pin = _draw_fallback(card)
    else:
        draw = ImageDraw.Draw(canvas, 'RGBA')
        font = _font(400, 15)
        text_w = font.getlength(MAP_ATTRIBUTION)
        draw.rectangle((WIDTH - text_w - 20, HEIGHT - 28, WIDTH, HEIGHT), fill=(10, 11, 16, 170))
        draw.text((WIDTH - text_w - 10, HEIGHT - 24), MAP_ATTRIBUTION, font=font, fill=TEXT_DIM)
    if pin is not None:
        _draw_pin(canvas, pin[0], pin[1], card)
    _draw_text_panel(canvas, card)
    output = io.BytesIO()
    canvas.save(output, 'JPEG', quality=86, optimize=True, progressive=True)
    return output.getvalue(), used_map
