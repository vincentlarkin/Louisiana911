"""Caddo 911 source adapter."""

from __future__ import annotations

import re

import requests
from bs4 import BeautifulSoup


BASE_URL = "https://ias.ecc.caddo911.com/All_ActiveEvents.aspx"


def scrape(*, user_agent: str, timeout_seconds: int = 15) -> tuple[list[dict], str | None]:
    """
    Scrape active incidents from Caddo 911.

    Returns:
      (incidents, refreshed_at_text)
    """
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": user_agent,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.5",
            "Connection": "keep-alive",
        }
    )

    # First request establishes ASP.NET cookies.
    response = session.get(BASE_URL, timeout=timeout_seconds, allow_redirects=True)
    if "AspxAutoDetectCookieSupport" in response.url or response.status_code == 302:
        response = session.get(f"{BASE_URL}?AspxAutoDetectCookieSupport=1", timeout=timeout_seconds)
    response.raise_for_status()

    soup = BeautifulSoup(response.text, "html.parser")
    incidents: list[dict] = []
    refreshed_at_text: str | None = None

    strings = list(soup.stripped_strings)
    for index, text in enumerate(strings):
        if "Refreshed at:" in text:
            value = text.split("Refreshed at:", 1)[1].strip()
            # The live page puts the label in <b> and its value in a sibling.
            if not value and index + 1 < len(strings):
                value = strings[index + 1]
            if re.fullmatch(r"[A-Za-z]+\s+\d{1,2}\s+\d{1,2}:\d{2}", value):
                refreshed_at_text = value
            break

    table = soup.find("table", id="ctl00_MainContent_GV_AE_ALL_P")
    expected_headers = ["Agency", "Time", "Units", "Description", "Street", "Cross Streets", "Mun"]
    if (table is None or not refreshed_at_text
            or [cell.get_text(strip=True) for cell in table.find_all("th")] != expected_headers):
        raise ValueError("Caddo active-event table or refresh marker is missing or unrecognized")

    for row in table.find_all("tr"):
        cells = row.find_all("td")
        if not cells:
            continue
        if len(cells) != 7:
            raise ValueError("Caddo active-event row has an unexpected layout")

        agency = cells[0].get_text(strip=True)
        time_val = cells[1].get_text(strip=True)
        units = cells[2].get_text(strip=True)
        description = cells[3].get_text(strip=True)
        street = cells[4].get_text(strip=True)
        cross_streets = cells[5].get_text(strip=True)
        municipality = cells[6].get_text(strip=True) if len(cells) > 6 else ""

        # Reject partial snapshots rather than clearing unparsed calls.
        if not (
            agency
            and len(agency) <= 10
            and time_val
            and time_val.isdigit()
            and len(time_val) <= 4
            and description
        ):
            raise ValueError("Caddo active-event row could not be parsed")

        clock = time_val.zfill(4)
        if int(clock[:2]) > 23 or int(clock[2:]) > 59:
            raise ValueError("Caddo active-event time is invalid")

        incidents.append(
            {
                "source": "caddo",
                "agency": agency,
                "time": time_val,
                "units": int(units) if units.isdigit() else 1,
                "description": description,
                "street": street,
                "cross_streets": cross_streets,
                "municipality": municipality,
            }
        )

    return incidents, refreshed_at_text
