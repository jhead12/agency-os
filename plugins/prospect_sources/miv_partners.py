"""
Mobilize the Immigrant Vote (MIV) California — partner CBOs.

MIV is a statewide coalition of 100+ CBOs doing immigrant voter
registration, education, and mobilization. These are the highest-quality
prospects for a voter guide product — they are literally doing voter
engagement right now.

Source: mivcalifornia.org partner list.
Free public data — no API key needed.
"""

from __future__ import annotations

import re
from typing import Iterator

import httpx
from selectolax.parser import HTMLParser

from core.models import Prospect


# Known MIV partner organizations (from the published partner list).
# This is a curated seed list — the full list has 100+ orgs.
MIV_PARTNERS = [
    "9 to 5 Los Angeles",
    "ACLU of Southern California",
    "ACCE San Diego",
    "ACCE Inland Empire",
    "Asian Pacific American Legal Center",
    "Arab Resource and Organizing Center",
    "Bay Area Iranian-American Voter Association",
    "California Immigrant Policy Center",
    "California Rural Legal Assistance Foundation",
    "Caminos - Pathways Learning Center",
    "Canal Alliance",
    "California Partnership",
    "CARECEN",
    "Catholic Charities",
    "Catholic Charities of Diocese of Santa Rosa",
    "CAUSE",
    "Central Coast Organizing Project",
    "Center for Political Education",
    "Centro Bellas Artes",
    "Centro La Familia Advocacy",
    "Cesar E. Chavez Foundation",
    "CET Immigration & Citizenship Program",
    "CHAM Deliverance Ministry",
    "Chinatown Community Development Center",
    "Chinese for Affirmative Action",
    "Chinese Progressive Association",
    "Coalition for Humane Immigrant Rights of Los Angeles",
    "Church of Peace",
    "Community Coalition",
    "Grassroots Health Care Los Angeles",
    "IDEAS at Mt. Sac",
    "Immigrant Legal Resource Center",
    "Independent Living Resource Center",
    "InnerCity Struggles",
    "International Institute of the Bay Area",
    "Korean Community Center of the East Bay",
    "Koreatown Immigrant Workers Alliance",
    "Mid-City CAN",
    "Mujeres Unidas y Activas",
    "National Korean American Service & Education Consortium",
    "NAKASEC",
    "Narika",
    "National Network for Immigrant and Refugee Rights",
    "Orange County Korean-U.S. Citizens League",
    "Orange County Asian and Pacific Islander Community Alliance",
    "OCAPICA",
    "PODER",
    "PUEBLO",
    "Quetzal Services",
    "Riverside Latino Voter Project",
    "Rose Foundation for Communities and the Environment",
    "S.U.R.G.E. at CSULA",
    "San Diego Housing Federation",
    "San Jose Conservation Corps and Charter School",
    "Santa Cruz County Immigration Project",
    "Silverlake Hollywood Echo Park Metropolitan Alliance",
    "SOMCAN",
    "Somos Mayfair",
    "St. Margaret's Center / Catholic Charities",
    "Students for Equality in Education",
    "Supportive Parents Informative Network",
    "Teatro Vision",
    "The Wall Las Memorias",
    "VOICES at GCC",
    "Watsonville Brown Berets",
    "Welfare Warriors",
    "Whistlestop",
    "Youth United for Community Action",
]


class MivPartnersSource:
    """Mobilize the Immigrant Vote California partner CBOs."""

    key = "miv_partners"
    URL = "https://www.mivcalifornia.org/docs/Mobilize_the_Immigrant_Vote/"

    def is_configured(self) -> bool:
        return True

    def discover(self, filters: dict) -> Iterator[Prospect]:
        state_filter = filters.get("state", "CA")
        county_filter = filters.get("county")

        # Try scraping the live page first
        scraped = list(self._scrape(state_filter, county_filter))
        if scraped:
            yield from scraped
            return

        # Fallback: use the curated list
        for name in MIV_PARTNERS:
            # Filter to LA County if requested (heuristic: orgs with "Los Angeles" in name)
            if county_filter and county_filter.lower() == "los angeles":
                if "los angeles" not in name.lower() and "la " not in name.lower():
                    # Still include — many LA-based orgs don't have LA in name
                    pass

            yield Prospect(
                name=name,
                state=state_filter,
                county=county_filter,
                source=self.key,
                source_url=self.URL,
                voter_engagement=True,
                focus_area="immigrant_voter_mobilization",
            )

    def _scrape(self, state: str, county: str | None) -> Iterator[Prospect]:
        """Attempt to scrape the MIV partner list from the live page."""
        try:
            resp = httpx.get(self.URL, timeout=30, follow_redirects=True)
            resp.raise_for_status()
        except httpx.HTTPError:
            return

        tree = HTMLParser(resp.text)
        seen: set[str] = set()

        # The partner list is typically in <li> or <p> tags
        for node in tree.css("li, p, td"):
            text = node.text(strip=True)
            if not text or len(text) < 5 or len(text) > 200:
                continue
            # Clean up trailing dashes/ellipses
            text = re.sub(r"\s*[-–—…]\s*$", "", text).strip()
            if text in seen:
                continue
            # Filter out obvious non-org text
            if text.startswith(("##", "*/", "MIV", "Mobilize", "Campaign", "Coordinating")):
                continue
            seen.add(text)
            yield Prospect(
                name=text,
                state=state,
                county=county,
                source=self.key,
                source_url=self.URL,
                voter_engagement=True,
                focus_area="immigrant_voter_mobilization",
            )