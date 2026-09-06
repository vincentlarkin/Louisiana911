"""Lafayette 911 traffic source adapter."""

from __future__ import annotations

from datetime import datetime, timezone
import re
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup


FEED_URL = "https://lafayette911.org/wp-json/traffic-feed/v1/data"
CENTRAL_TZ = ZoneInfo("America/Chicago")

# The feed renders the municipality on a new line, but HTML text extraction
# collapses that line into the preceding cross street. Match known service-area
# names at the end instead of treating the whole uppercase suffix as a city.
MUNICIPALITY_SUFFIXES = (
    "LAFAYETTE",
    "BROUSSARD",
    "CARENCRO",
    "DUSON",
    "SCOTT",
    "YOUNGSVILLE",
)


def _clean_ws(value: str | None) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def _split_location(value: str | None) -> tuple[str, str, str]:
    """
    Parse Lafayette's 'Located At' field into (street, cross_streets, municipality).
    """
    raw = _clean_ws(value)
    if not raw:
        return "", "", ""

    municipality = ""
    base = raw
    city_pattern = "|".join(re.escape(name) for name in MUNICIPALITY_SUFFIXES)
    city_match = re.search(
        rf"\b({city_pattern})\s*,\s*LA\s*$",
        base,
        flags=re.IGNORECASE,
    )
    if city_match:
        municipality = _clean_ws(city_match.group(1)).upper()
        base = _clean_ws(base[: city_match.start()])

    if "/" in base:
        left, right = base.split("/", 1)
        street = _clean_ws(left)
        cross_streets = _clean_ws(right)
    else:
        street = base
        cross_streets = ""

    return street, cross_streets, municipality


def _normalize_assisting(value: str | None) -> str:
    raw = _clean_ws(value).upper()
    if not raw:
        return ""

    # Keep only known assisting units in feed order and present consistently.
    matches = list(re.finditer(r"\b(FIRE|POLICE|SHERIFF)\b", raw))
    if not matches:
        return raw

    ordered_units: list[str] = []
    seen: set[str] = set()
    for match in matches:
        unit = match.group(1)
        if unit in seen:
            continue
        seen.add(unit)
        ordered_units.append(unit)
    return " / ".join(ordered_units)


def scrape(*, user_agent: str, timeout_seconds: int = 15) -> tuple[list[dict], str | None]:
    response = requests.get(
        FEED_URL,
        headers={"User-Agent": user_agent, "Accept": "application/json"},
        timeout=timeout_seconds,
    )
    response.raise_for_status()

    payload = response.json()
    if not isinstance(payload, dict) or payload.get("success") is not True or not isinstance(payload.get("data"), str):
        raise ValueError("Lafayette traffic request did not return a successful snapshot")

    html = payload.get("data") or ""
    soup = BeautifulSoup(html, "html.parser")
    table = soup.find("table")
    expected_headers = ["located at", "due to", "reported at", "assisting"]
    header = table.find("tr") if table else None
    if (header is None or [_clean_ws(cell.get_text(" ", strip=True)).lower()
                          for cell in header.find_all(["td", "th"])] != expected_headers):
        raise ValueError("Lafayette traffic table is unrecognized")

    incidents: list[dict] = []
    rows = table.find_all("tr")[1:]
    for row in rows:
        cells = row.find_all("td")
        if len(cells) != 4:
            raise ValueError("Lafayette traffic row has an unexpected layout")

        location_raw = cells[0].get_text(" ", strip=True)
        description = _clean_ws(cells[1].get_text(" ", strip=True))
        reported_text = _clean_ws(cells[2].get_text(" ", strip=True))
        # Preserve the source's full date, including calls spanning many days.
        reported = datetime.strptime(reported_text, "%m/%d/%Y %H:%M").replace(tzinfo=CENTRAL_TZ)
        time_val = reported.strftime("%H%M")
        agency = _normalize_assisting(cells[3].get_text(" ", strip=True))
        street, cross_streets, municipality = _split_location(location_raw)

        if not description:
            raise ValueError("Lafayette traffic row has no description")

        incidents.append(
            {
                "source": "lafayette",
                "agency": agency or "UNKNOWN",
                "time": time_val,
                "occurred_at": reported.astimezone(timezone.utc).isoformat(),
                "units": 1,
                "description": description,
                "street": street,
                "cross_streets": cross_streets,
                "municipality": municipality,
            }
        )

    # Endpoint does not expose a dedicated refreshed timestamp.
    return incidents, None
