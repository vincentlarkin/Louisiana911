"""Lake Charles Police-to-Citizen public closed CAD calls.

Use the public site's normal anonymous session and paged search. Never request
open calls or override the agency's rolling publication window. This is the
website's API, not a versioned partner contract; fail closed on schema changes.
"""

from datetime import datetime, timedelta, timezone
import math
import re
from zoneinfo import ZoneInfo

import requests

BASE_URL = 'https://lakecharles.policetocitizen.com'
PUBLIC_URL = BASE_URL + '/cadcalls'
PAGE_SIZE = 200  # The public client's maximum page size.
MAX_PAGES = 10
RECENT_LIMIT = 150
RECENT_DAYS = 7
CENTRAL_TZ = ZoneInfo('America/Chicago')


def _text(value):
    return re.sub(r'\s+', ' ', str(value or '')).strip()


def _timestamp(value):
    try:
        parsed = datetime.fromisoformat(str(value))
        return parsed if parsed.tzinfo is not None else None
    except (ValueError, TypeError):
        return None


def _normalize_row(row, *, now):
    if not isinstance(row, dict):
        raise ValueError('Unexpected Lake Charles call record')
    # The host also advertises consortium agencies. The Lake Charles tab covers
    # only LCPD; a shared host must not silently expand its geographic scope.
    if row.get('CallType') != 'CLOSED' or row.get('Agency') != 'LCPD':
        return None
    event_id = _text(row.get('IncidentId'))
    started = _timestamp(row.get('StartTime'))
    ended = _timestamp(row.get('EndTime'))
    nature = _text(row.get('Nature'))
    if not event_id or not started or not ended or not nature or ended < started:
        raise ValueError('Incomplete Lake Charles closed call')
    # Add a local delay check in addition to the agency's publication rules.
    if ended > now - timedelta(hours=24):
        return None
    lat = lon = None
    if row.get('HasLocation') is True:
        try:
            y, x = float(row['Latitude']), float(row['Longitude'])
            if math.isfinite(y) and math.isfinite(x) and 30.0 <= y <= 30.45 and -93.4 <= x <= -92.95:
                lat, lon = y, x
        except (KeyError, TypeError, ValueError):
            pass
    return {
        'source': 'lakecharles',
        'source_id': f'LCPD:{event_id}',
        'agency': 'LCPD',
        'time': started.astimezone(CENTRAL_TZ).strftime('%H%M'),
        'units': 0,  # Not published; the UI displays this as unknown.
        'description': re.sub(r'^L-', '', nature),
        'street': _text(row.get('Address')),
        'cross_streets': '',
        'municipality': 'Lake Charles',
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
            raise ValueError('Lake Charles public session unavailable')
        session.headers['X-XSRF-TOKEN'] = token
        initial = session.get(BASE_URL + '/api/Agency/InitialSettings', timeout=timeout_seconds)
        initial.raise_for_status()
        agency = initial.json()
        if not isinstance(agency, dict) or agency.get('Name') != 'lakecharles' or agency.get('IsDeleted'):
            raise ValueError('Lake Charles agency configuration changed')
        agency_id = agency.get('AgencyId')
        if type(agency_id) is not int or agency_id <= 0:
            raise ValueError('Invalid Lake Charles agency ID')
        settings = session.get(BASE_URL + f'/api/CADCalls/ADSSettings/{agency_id}', timeout=timeout_seconds)
        settings.raise_for_status()
        if settings.json().get('ClosedCallsEnabled') is not True:
            raise ValueError('Lake Charles closed calls are unavailable')

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
                raise ValueError('Unexpected Lake Charles response')
            rows, total = payload.get('CADCalls'), payload.get('Total')
            if not isinstance(rows, list) or type(total) is not int or total < 0 or total > PAGE_SIZE * MAX_PAGES:
                raise ValueError('Lake Charles response exceeds safe import bounds')
            if len(rows) > PAGE_SIZE or (not rows and offset < total):
                raise ValueError('Incomplete Lake Charles page')
            new_keys = set()
            for row in rows:
                incident = _normalize_row(row, now=now)
                key = (_text(row.get('Agency')), _text(row.get('IncidentId')))
                new_keys.add(key)
                if incident:
                    incidents[incident['source_id']] = incident
            if rows and not (new_keys - seen_rows):
                raise ValueError('Lake Charles repeated a page')
            seen_rows.update(new_keys)
            offset += len(rows)
            if offset >= total:
                break
            if len(rows) < PAGE_SIZE:
                raise ValueError('Truncated Lake Charles search')
        else:
            raise ValueError('Lake Charles pagination limit reached')

    normalized = sorted(incidents.values(), key=lambda row: (row['occurred_at'], row['source_id']), reverse=True)
    label = 'No published closed calls'
    if normalized:
        newest = datetime.fromisoformat(normalized[0]['occurred_at']).astimezone(CENTRAL_TZ)
        label = newest.strftime('%b %d, %H:%M %Z')
    return normalized, label
