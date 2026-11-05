"""Sulphur Police-to-Citizen public closed CAD calls.

Use the public site's normal anonymous session and paged search. Never request
open calls or override the agency's rolling publication window. This is the
website's API, not a versioned partner contract; fail closed on schema changes.
"""

from datetime import datetime, timedelta, timezone
import math
import re
from zoneinfo import ZoneInfo

import requests

from sources.p2c_time import repair_fall_back_start

BASE_URL = 'https://sulphur.policetocitizen.com'
PUBLIC_URL = BASE_URL + '/cadcalls'
AGENCY_CODE = 'SPD'
PAGE_SIZE = 200  # The public client's maximum page size.
MAX_PAGES = 10
RECENT_LIMIT = 150
RECENT_DAYS = 7
CENTRAL_TZ = ZoneInfo('America/Chicago')
# Sulphur PD answers calls in Sulphur, Westlake and the unincorporated area
# between them, so the box is wider than the city limits.
LAT_MIN, LAT_MAX = 30.08, 30.35
LON_MIN, LON_MAX = -93.55, -93.10
# Cities that appear as the last part of a published address.
KNOWN_CITIES = {'SULPHUR': 'Sulphur', 'WESTLAKE': 'Westlake', 'LAKE CHARLES': 'Lake Charles',
                'CARLYSS': 'Carlyss', 'VINTON': 'Vinton', 'MOSS BLUFF': 'Moss Bluff'}


def _text(value):
    return re.sub(r'\s+', ' ', str(value or '')).strip()


def _timestamp(value):
    try:
        parsed = datetime.fromisoformat(str(value))
        return parsed if parsed.tzinfo is not None else None
    except (ValueError, TypeError):
        return None


def _municipality(address):
    tail = address.rsplit(',', 1)[-1].strip().upper() if ',' in address else ''
    return KNOWN_CITIES.get(tail, 'Sulphur')


def _normalize_row(row, *, now):
    if not isinstance(row, dict):
        raise ValueError('Unexpected Sulphur call record')
    if row.get('CallType') != 'CLOSED' or row.get('Agency') != AGENCY_CODE:
        return None
    event_id = _text(row.get('IncidentId'))
    started = _timestamp(row.get('StartTime'))
    ended = _timestamp(row.get('EndTime'))
    started = repair_fall_back_start(started, ended, CENTRAL_TZ)
    nature = _text(row.get('Nature'))
    if not event_id or not started or not ended or not nature or ended < started:
        raise ValueError('Incomplete Sulphur closed call')
    # Add a local delay check in addition to the agency's publication rules.
    if ended > now - timedelta(hours=24):
        return None
    lat = lon = None
    if row.get('HasLocation') is True:
        try:
            y, x = float(row['Latitude']), float(row['Longitude'])
            if math.isfinite(y) and math.isfinite(x) and LAT_MIN <= y <= LAT_MAX and LON_MIN <= x <= LON_MAX:
                lat, lon = y, x
        except (KeyError, TypeError, ValueError):
            pass
    address = _text(row.get('Address'))
    return {
        'source': 'sulphur',
        'source_id': f'{AGENCY_CODE}:{event_id}',
        'agency': AGENCY_CODE,
        'time': started.astimezone(CENTRAL_TZ).strftime('%H%M'),
        'units': 0,  # Not published; the UI displays this as unknown.
        # The CAD prefixes natures with L- (law), F- (fire) or E- (EMS).
        'description': re.sub(r'^[LFE]-', '', nature),
        'street': address,
        'cross_streets': '',
        'municipality': _municipality(address),
        'latitude': lat,
        'longitude': lon,
        'coordinates_published': lat is not None,
        'location_is_approximate': True,
        'occurred_at': started.astimezone(timezone.utc).isoformat(),
        'is_active': False,
    }


def scrape(*, user_agent, timeout_seconds=20, now=None):
    now = now or datetime.now(timezone.utc)
    with requests.Session() as session:
        session.headers.update({'User-Agent': user_agent, 'Accept': 'application/json'})
        response = session.get(PUBLIC_URL, timeout=timeout_seconds)
        response.raise_for_status()
        # The public site's standard double-submit CSRF cookie is required for
        # its read-only POST search. No account/session credentials are stored.
        token = session.cookies.get('XSRF-TOKEN')
        if not token:
            raise ValueError('Sulphur public session unavailable')
        session.headers['X-XSRF-TOKEN'] = token
        initial = session.get(BASE_URL + '/api/Agency/InitialSettings', timeout=timeout_seconds)
        initial.raise_for_status()
        agency = initial.json()
        if not isinstance(agency, dict) or agency.get('Name') != 'sulphur' or agency.get('IsDeleted'):
            raise ValueError('Sulphur agency configuration changed')
        agency_id = agency.get('AgencyId')
        if type(agency_id) is not int or agency_id <= 0:
            raise ValueError('Invalid Sulphur agency ID')
        settings = session.get(BASE_URL + f'/api/CADCalls/ADSSettings/{agency_id}', timeout=timeout_seconds)
        settings.raise_for_status()
        if settings.json().get('ClosedCallsEnabled') is not True:
            raise ValueError('Sulphur closed calls are unavailable')

        incidents = {}
        seen_rows = set()
        offset = 0
        for _ in range(MAX_PAGES):
            response = session.post(BASE_URL + f'/api/CADCalls/{agency_id}', json={
                'IncludeOpenCalls': False, 'IncludeClosedCalls': True, 'IncludeCount': True,
                'PagingOptions': {'Take': PAGE_SIZE, 'Skip': offset, 'SortOptions': [
                    {'Name': 'StartTime', 'SortDirection': 'Descending', 'Sequence': 1},
                    {'Name': 'IncidentId', 'SortDirection': 'Descending', 'Sequence': 2},
                ]},
                'FilterOptionsParameters': {'IntersectionSearch': True, 'SearchText': '', 'Parameters': []},
            }, timeout=timeout_seconds)
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, dict):
                raise ValueError('Unexpected Sulphur response')
            rows, total = payload.get('CADCalls'), payload.get('Total')
            if not isinstance(rows, list) or type(total) is not int or total < 0 or total > PAGE_SIZE * MAX_PAGES:
                raise ValueError('Sulphur response exceeds safe import bounds')
            if len(rows) > PAGE_SIZE or (not rows and offset < total):
                raise ValueError('Incomplete Sulphur page')
            new_keys = set()
            for row in rows:
                incident = _normalize_row(row, now=now)
                key = (_text(row.get('Agency')), _text(row.get('IncidentId')))
                new_keys.add(key)
                if incident:
                    incidents[incident['source_id']] = incident
            if rows and not (new_keys - seen_rows):
                raise ValueError('Sulphur repeated a page')
            seen_rows.update(new_keys)
            offset += len(rows)
            if offset >= total:
                break
            if len(rows) < PAGE_SIZE:
                raise ValueError('Truncated Sulphur search')
        else:
            raise ValueError('Sulphur pagination limit reached')

    normalized = sorted(incidents.values(), key=lambda row: (row['occurred_at'], row['source_id']), reverse=True)
    label = 'No published closed calls'
    if normalized:
        newest = datetime.fromisoformat(normalized[0]['occurred_at']).astimezone(CENTRAL_TZ)
        label = newest.strftime('%b %d, %H:%M %Z')
    return normalized, label
