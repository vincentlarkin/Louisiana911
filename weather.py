"""Current conditions for each covered area, for the Android app's weather blip.

The server asks the National Weather Service for the latest observation at
one airport per area, at most every 15 minutes, and hands the app a few
hundred bytes. The phone picks the nearest area itself, so no one's location
is sent anywhere for this.
"""
import time
from threading import Lock, Thread

import requests

# One NWS observation station per area key (the app's Area keys).
STATIONS = {
    'caddo': 'KSHV',        # Shreveport Regional
    'batonrouge': 'KBTR',   # Baton Rouge Metro
    'lafayette': 'KLFT',    # Lafayette Regional
    'neworleans': 'KMSY',   # New Orleans International
    'calcasieu': 'KLCH',    # Lake Charles Regional (Lake Charles and Sulphur)
}
REFRESH_SECONDS = 15 * 60
MAX_AGE_SECONDS = 2 * 3600  # older observations are dropped, not shown
USER_AGENT = 'Louisiana911 (https://louisiana911.com)'
URL = 'https://api.weather.gov/stations/{station}/observations/latest'

_lock = Lock()
_cache: dict = {'at': 0.0, 'areas': {}}
_refreshing = False


def sky_from_icon(icon_url: str | None) -> str:
    """NWS icon URL (…/land/day/bkn?size=medium) -> a small set of sky words."""
    if not icon_url:
        return 'unknown'
    try:
        code = icon_url.split('?', 1)[0].rstrip('/').split('/')[-1].split(',')[0]
    except (AttributeError, IndexError):
        return 'unknown'
    night = '/night/' in icon_url
    if code.startswith('tsra') or code.startswith('hurricane') or code.startswith('tropical'):
        return 'storm'
    if code in {'snow', 'blizzard', 'sleet', 'ra_sn', 'ra_fzra', 'fzra', 'snow_sleet', 'snow_fzra', 'cold'}:
        return 'snow'
    if code.startswith('ra') or code.startswith('minus_ra') or code.startswith('shra') or 'rain' in code:
        return 'rain'
    if code in {'fg', 'fog', 'hz', 'haze', 'fu', 'smoke', 'du', 'dust'}:
        return 'fog'
    if code in {'ovc', 'bkn', 'wind_bkn', 'wind_ovc'}:
        return 'cloudy'
    if code in {'sct', 'few', 'wind_sct', 'wind_few'}:
        return 'partly-night' if night else 'partly'
    if code in {'skc', 'wind_skc', 'hot'}:
        return 'clear-night' if night else 'clear'
    return 'unknown'


def summarize(payload: dict, now: float | None = None) -> dict | None:
    """One NWS observation -> {'tempF', 'sky', 'text', 'at'} or None if unusable."""
    props = (payload or {}).get('properties') or {}
    celsius = (props.get('temperature') or {}).get('value')
    stamp = props.get('timestamp')
    if celsius is None or not stamp:
        return None
    try:
        from datetime import datetime
        observed = datetime.fromisoformat(stamp.replace('Z', '+00:00')).timestamp()
    except ValueError:
        return None
    now = time.time() if now is None else now
    if now - observed > MAX_AGE_SECONDS:
        return None
    return {
        'tempF': round(float(celsius) * 9 / 5 + 32),
        'sky': sky_from_icon(props.get('icon')),
        'text': (props.get('textDescription') or '').strip()[:40],
        'at': stamp,
    }


def _refresh() -> None:
    global _refreshing
    areas = {}
    for area, station in STATIONS.items():
        try:
            response = requests.get(
                URL.format(station=station),
                headers={'User-Agent': USER_AGENT, 'Accept': 'application/geo+json'},
                timeout=8,
            )
            response.raise_for_status()
            summary = summarize(response.json())
        except (requests.RequestException, ValueError):
            summary = None
        if summary:
            areas[area] = summary
    with _lock:
        if areas:
            _cache['areas'] = areas
        # Even a failed round waits the full interval, so an NWS outage
        # never turns into a request storm.
        _cache['at'] = time.time()
        _refreshing = False


def current() -> dict:
    """Cached conditions; starts a background refresh when they're stale."""
    global _refreshing
    with _lock:
        stale = time.time() - _cache['at'] > REFRESH_SECONDS
        if stale and not _refreshing:
            _refreshing = True
            Thread(target=_refresh, daemon=True).start()
        return dict(_cache['areas'])
