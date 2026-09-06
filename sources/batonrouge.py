"""Baton Rouge traffic incident source adapter."""

from __future__ import annotations

from datetime import datetime
import re

import requests
from bs4 import BeautifulSoup


FEED_URL = "https://city.brla.gov/traffic/incidents.asp"
TRAFFIC_MAP_QUERY_URL = (
    "https://maps.brla.gov/gis/rest/services/Transportation/"
    "Traffic_Incident/MapServer/0/query"
)


def _clean_ws(value: str | None) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def _parse_time_to_hhmm(value: str | None) -> str:
    text = _clean_ws(value)
    if not text:
        return ""

    for fmt in ("%I:%M:%S %p", "%I:%M %p"):
        try:
            return datetime.strptime(text, fmt).strftime("%H%M")
        except Exception:
            pass

    # Fallback if upstream formatting shifts.
    match = re.search(r"(\d{1,2}):(\d{2})(?::\d{2})?\s*([AP]M)", text, flags=re.IGNORECASE)
    if not match:
        return ""

    hour = int(match.group(1))
    minute = int(match.group(2))
    ampm = match.group(3).upper()
    if not (1 <= hour <= 12 and 0 <= minute <= 59):
        return ""
    if hour == 12:
        hour = 0
    if ampm == "PM":
        hour += 12
    if 0 <= hour <= 23 and 0 <= minute <= 59:
        return f"{hour:02d}{minute:02d}"
    return ""


def _extract_refreshed_at_text(soup: BeautifulSoup) -> str | None:
    text = _clean_ws(soup.get_text(" ", strip=True))
    if not text:
        return None
    match = re.search(r"Last Updated\s+(.+?)\s+Number of incidents\s*:", text, flags=re.IGNORECASE)
    if match:
        return _clean_ws(match.group(1))
    return None


def _split_location(location: str, cross_street: str) -> tuple[str, str]:
    """Normalize the feed's address and intersection variants."""
    location_clean = _clean_ws(location)
    cross_clean = _clean_ws(cross_street)

    # A location such as "HOOPER RD / SULLIVAN RD" is an intersection, while
    # a leading house number is a normal street address and should stay whole.
    if not re.match(r"^\d+[A-Z-]*\s+", location_clean, flags=re.IGNORECASE):
        parts = [_clean_ws(part) for part in location_clean.split("/", 1)]
        if len(parts) == 2 and all(parts):
            return parts[0], parts[1]

    return location_clean, cross_clean


def _incident_match_key(
    description: str | None,
    street: str | None,
    cross_streets: str | None,
) -> tuple[str, str, str]:
    """Build a formatting-insensitive key shared by the HTML and map feeds."""
    return tuple(
        re.sub(r"[^A-Z0-9]+", "", _clean_ws(value).upper())
        for value in (description, street, cross_streets)
    )


def _published_coordinates(feature: dict) -> tuple[float, float] | None:
    """Read a WGS84 point from the official EBR traffic-map feature."""
    geometry = feature.get("geometry") if isinstance(feature, dict) else None
    attributes = feature.get("attributes") if isinstance(feature, dict) else None
    geometry = geometry if isinstance(geometry, dict) else {}
    attributes = attributes if isinstance(attributes, dict) else {}

    raw_lon = geometry.get("x", attributes.get("LON"))
    raw_lat = geometry.get("y", attributes.get("LAT"))
    try:
        latitude = float(raw_lat)
        longitude = float(raw_lon)
    except (TypeError, ValueError):
        return None

    # The service is scoped to East Baton Rouge Parish. Keep a second local
    # guard so a changed spatial reference cannot silently create bad points.
    if not (30.15 < latitude < 30.80 and -91.50 < longitude < -90.90):
        return None
    return latitude, longitude


def _merge_published_points(incidents: list[dict], features: list[dict]) -> None:
    """Attach approximate points from the City-Parish's public traffic map."""
    points_by_key: dict[tuple[str, str, str], list[tuple[float, float]]] = {}
    for feature in features:
        attributes = feature.get("attributes") if isinstance(feature, dict) else None
        if not isinstance(attributes, dict):
            continue
        coordinates = _published_coordinates(feature)
        if not coordinates:
            continue
        key = _incident_match_key(
            attributes.get("INCIDENT_TYPE_DESC"),
            attributes.get("ADDRESS"),
            attributes.get("CROSS_STREET"),
        )
        points_by_key.setdefault(key, []).append(coordinates)

    for incident in incidents:
        # City-Parish warns that all traffic-incident points are approximate.
        incident["location_is_approximate"] = True
        key = _incident_match_key(
            incident.get("description"),
            incident.get("street"),
            incident.get("cross_streets"),
        )
        matches = points_by_key.get(key)
        if not matches:
            continue
        latitude, longitude = matches.pop(0)
        incident["latitude"] = latitude
        incident["longitude"] = longitude
        incident["coordinates_published"] = True


def _fetch_published_features(*, user_agent: str, timeout_seconds: int) -> list[dict]:
    response = requests.get(
        TRAFFIC_MAP_QUERY_URL,
        params={
            "f": "json",
            "where": "1=1",
            "outFields": "INCIDENT_TYPE_DESC,ADDRESS,CROSS_STREET,LAT,LON",
            "returnGeometry": "true",
            "outSR": "4326",
        },
        headers={
            "User-Agent": user_agent,
            "Accept": "application/json",
        },
        timeout=timeout_seconds,
    )
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict) or payload.get("error"):
        return []
    features = payload.get("features")
    return features if isinstance(features, list) else []


def scrape(*, user_agent: str, timeout_seconds: int = 15) -> tuple[list[dict], str | None]:
    response = requests.get(
        FEED_URL,
        headers={
            "User-Agent": user_agent,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        },
        timeout=timeout_seconds,
    )
    response.raise_for_status()

    soup = BeautifulSoup(response.text, "html.parser")
    refreshed_at_text = _extract_refreshed_at_text(soup)
    count_match = re.search(r"Number of incidents\s*:\s*(\d+)",
                            soup.get_text(" ", strip=True), re.IGNORECASE)
    table = soup.find("table", class_="TabTraf")
    expected_headers = ["Time", "Type", "Agency", "Location", "Cross Street"]
    if (not refreshed_at_text or not count_match or table is None
            or [_clean_ws(cell.get_text(" ", strip=True)) for cell in table.find_all("th")] != expected_headers):
        raise ValueError("Baton Rouge traffic table or snapshot count is unrecognized")

    incidents: list[dict] = []
    for row in table.find_all("tr"):
        cells = row.find_all("td")
        if not cells:
            continue
        if len(cells) != 5:
            raise ValueError("Baton Rouge traffic row has an unexpected layout")

        time_raw = _clean_ws(cells[0].get_text(" ", strip=True))
        incident_type = _clean_ws(cells[1].get_text(" ", strip=True))
        agency = _clean_ws(cells[2].get_text(" ", strip=True))
        location = _clean_ws(cells[3].get_text(" ", strip=True))
        cross_street = _clean_ws(cells[4].get_text(" ", strip=True))

        time_val = _parse_time_to_hhmm(time_raw)
        if not incident_type or not time_val:
            raise ValueError("Baton Rouge traffic row could not be parsed")

        street, cross_streets = _split_location(location, cross_street)

        incidents.append(
            {
                "source": "batonrouge",
                "agency": agency or "UNKNOWN",
                "time": time_val,
                "units": 1,
                "description": incident_type,
                "street": street,
                "cross_streets": cross_streets,
                "municipality": "Baton Rouge",
            }
        )

    if len(incidents) != int(count_match.group(1)):
        raise ValueError("Baton Rouge traffic snapshot is incomplete")

    if not incidents:
        return incidents, refreshed_at_text

    # The City-Parish publishes the same active CAD traffic incidents in an
    # official ArcGIS layer with approximate map points. Treat that layer as
    # best-effort enrichment: the readable incident list must keep working if
    # the map service is briefly unavailable or a just-added row is lagging.
    try:
        features = _fetch_published_features(
            user_agent=user_agent,
            timeout_seconds=timeout_seconds,
        )
    except Exception:
        features = []
    _merge_published_points(incidents, features)

    return incidents, refreshed_at_text
