"""Baton Rouge Police calls for service (public ArcGIS feature layer).

The City-Parish publishes BRPD's dispatched calls as a public hosted layer,
loaded in daily batches a day or two after the calls. This adapter reads a
recent window of that layer. It never treats a call as active.

Calls here include disturbances, welfare checks and other calls at homes, so
by default the address is reduced to its block and the map point is snapped to
a coarse grid, like the other delayed police feeds on the site.
"""

from datetime import datetime, timedelta, timezone
import math
import re
from zoneinfo import ZoneInfo

import requests

LAYER_URL = (
    'https://services7.arcgis.com/w9KefBd0vxjnp6IW/arcgis/rest/services/'
    'BRPD_Comm_Div_Summary_Data2/FeatureServer/0'
)
PUBLIC_URL = 'https://www.arcgis.com/home/item.html?id=6794bfcaaabe4576a7a9081e5d7925ae'
AGENCY_CODE = 'BRPD'
FIELDS = ('IncidentNumber', 'FinalProblemType', 'Address', 'City', 'CallDate', 'ClosedDate')
PAGE_SIZE = 1000
MAX_PAGES = 12
IMPORT_DAYS = 7  # Re-read window; wide enough to catch up after a holiday gap.
RECENT_LIMIT = 400  # About one published day of calls.
RECENT_DAYS = 7
CENTRAL_TZ = ZoneInfo('America/Chicago')
# East Baton Rouge Parish; a changed spatial reference must not create points.
LAT_MIN, LAT_MAX = 30.20, 30.75
LON_MIN, LON_MAX = -91.45, -90.95

# Show the block, not the house number, and a point within about 250 feet.
GENERALIZE_LOCATIONS = True
GRID_DEGREES = 0.001

# The live Baton Rouge traffic feed already carries these calls as they happen.
TRAFFIC_FEED_TYPES = re.compile(r'crash|hit\s*(?:&|and)\s*run|stalled\s+veh|traffic\s+congestion', re.IGNORECASE)
# Placeholder call types that say nothing about the call.
PLACEHOLDER_TYPES = re.compile(r'\bdefault\b', re.IGNORECASE)
CITY_NAMES = {'ST.GEORGE': 'St. George', 'ST GEORGE': 'St. George'}


def _text(value):
    return re.sub(r'\s+', ' ', str(value or '')).strip()


def _local_time(value):
    """The layer stores Central wall-clock time in a field labeled UTC."""
    if type(value) not in (int, float) or not math.isfinite(value):
        return None
    try:
        wall = datetime.fromtimestamp(value / 1000, timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None
    return wall.replace(tzinfo=CENTRAL_TZ)


def _description(problem_type):
    """Drop the CAD's sorting prefixes: '..LAW 911 HANG UP' -> '911 HANG UP'."""
    text = re.sub(r'^\.+', '', problem_type)
    text = re.sub(r'^(?:HP\s+)?LAW\s+', '', text, flags=re.IGNORECASE)
    return _text(text.replace('*', ' '))


def _block(address):
    """'4637 - 12 FLORIDA BLVD' -> '4600 BLK FLORIDA BLVD'."""
    match = re.match(r'^(\d+)(?:\s*-\s*\w+)?\s+(.+)$', address)
    if not match:
        return address
    return f'{int(match.group(1)) // 100 * 100} BLK {match.group(2)}'


def _location(address):
    """Return (street, cross_streets) from an address or an 'A ST / B ST' corner."""
    if '/' in address and not re.match(r'^\d+\s', address):
        street, cross = (_text(part) for part in address.split('/', 1))
        if street and cross:
            return street, cross
    return (_block(address) if GENERALIZE_LOCATIONS else address), ''


def _point(geometry):
    try:
        lat, lon = float(geometry['y']), float(geometry['x'])
    except (KeyError, TypeError, ValueError):
        return None, None
    if not (math.isfinite(lat) and math.isfinite(lon) and LAT_MIN <= lat <= LAT_MAX and LON_MIN <= lon <= LON_MAX):
        return None, None
    if GENERALIZE_LOCATIONS:
        lat = round(round(lat / GRID_DEGREES) * GRID_DEGREES, 6)
        lon = round(round(lon / GRID_DEGREES) * GRID_DEGREES, 6)
    return lat, lon


def _normalize_feature(feature, *, now):
    attributes = feature.get('attributes') if isinstance(feature, dict) else None
    if not isinstance(attributes, dict) or any(field not in attributes for field in FIELDS):
        raise ValueError('Unexpected Baton Rouge police call record')
    number = _text(attributes['IncidentNumber'])
    started = _local_time(attributes['CallDate'])
    if not number or not started:
        raise ValueError('Incomplete Baton Rouge police call')
    problem_type = _text(attributes['FinalProblemType'])
    if not problem_type or PLACEHOLDER_TYPES.search(problem_type) or TRAFFIC_FEED_TYPES.search(problem_type):
        return None
    # The layer is loaded a day or more late. Keep a local check as well, so a
    # faster upstream schedule could never make these calls look live.
    ended = _local_time(attributes['ClosedDate']) or started
    if max(started, ended) > now - timedelta(hours=24):
        return None
    street, cross_streets = _location(_text(attributes['Address']))
    lat, lon = _point(feature.get('geometry'))
    return {
        'source': 'brpd',
        'source_id': f'{AGENCY_CODE}:{number}',
        'agency': AGENCY_CODE,
        'time': started.strftime('%H%M'),
        'units': 0,  # Not published; the UI displays this as unknown.
        'description': _description(problem_type),
        'street': street,
        'cross_streets': cross_streets,
        'municipality': CITY_NAMES.get(_text(attributes['City']).upper(), 'Baton Rouge'),
        'latitude': lat,
        'longitude': lon,
        'coordinates_published': lat is not None,
        'location_is_approximate': GENERALIZE_LOCATIONS,
        'occurred_at': started.astimezone(timezone.utc).isoformat(),
        'is_active': False,
    }


def scrape(*, user_agent, timeout_seconds=20, now=None):
    now = now or datetime.now(timezone.utc)
    # CallDate holds Central wall-clock time, so the window is given the same way.
    since = (now.astimezone(CENTRAL_TZ) - timedelta(days=IMPORT_DAYS)).strftime('%Y-%m-%d %H:%M:%S')
    incidents = {}
    seen = set()
    offset = 0
    with requests.Session() as session:
        session.headers.update({'User-Agent': user_agent, 'Accept': 'application/json'})
        for _ in range(MAX_PAGES):
            response = session.get(LAYER_URL + '/query', params={
                'f': 'json',
                'where': f"CallDate >= TIMESTAMP '{since}'",
                'outFields': ','.join(FIELDS),
                'orderByFields': 'CallDate DESC, ObjectID DESC',
                'returnGeometry': 'true',
                'outSR': '4326',
                'resultOffset': offset,
                'resultRecordCount': PAGE_SIZE,
            }, timeout=timeout_seconds)
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, dict) or payload.get('error'):
                raise ValueError('Baton Rouge police layer returned an error')
            features = payload.get('features')
            if not isinstance(features, list) or len(features) > PAGE_SIZE:
                raise ValueError('Unexpected Baton Rouge police response')
            new_numbers = set()
            for feature in features:
                incident = _normalize_feature(feature, now=now)
                new_numbers.add(_text(feature['attributes']['IncidentNumber']))
                if incident:
                    incidents[incident['source_id']] = incident
            if features and not (new_numbers - seen):
                raise ValueError('Baton Rouge police layer repeated a page')
            seen.update(new_numbers)
            offset += len(features)
            if not payload.get('exceededTransferLimit'):
                break
            if not features:
                raise ValueError('Incomplete Baton Rouge police page')
        else:
            raise ValueError('Baton Rouge police pagination limit reached')

    normalized = sorted(incidents.values(), key=lambda row: (row['occurred_at'], row['source_id']), reverse=True)
    label = 'No published police calls'
    if normalized:
        newest = datetime.fromisoformat(normalized[0]['occurred_at']).astimezone(CENTRAL_TZ)
        label = newest.strftime('%b %d, %H:%M %Z')
    return normalized, label
