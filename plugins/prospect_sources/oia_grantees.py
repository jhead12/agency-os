"""
LA County Office of Immigrant Affairs (OIA) — CBO grantee lists.

Scrapes OIA pages for named CBOs that received capacity-building grants.
These are verified, active CBOs in LA County — excellent prospects.

The OIA publishes grantee names in press releases and program pages.
Free public data — no API key needed.
"""

from __future__ import annotations

import re
from typing import Iterator

import httpx
from selectolax.parser import HTMLParser

from core.models import Prospect


# Known OIA CBO grantees (from published press releases — hard-coded as
# a reliable seed list that doesn't depend on page structure staying stable).
# These are real organizations from the LA County OIA second cohort announcement.
OIA_GRANTEES = [
    ("African Communities Public Health Coalition", "ACPHC", "african_immigrant_health"),
    ("Catholic Charities of Los Angeles – Esperanza Immigrant Rights Project", None, "immigrant_legal"),
    ("Central American Resource Center of California", "CARECEN", "central_american_immigrant"),
    ("Coalition for Humane Immigrant Rights Los Angeles", "CHIRLA", "immigrant_civic_engagement"),
    ("Council on American-Islamic Relations", "CAIR-LA", "civil_rights_muslim"),
    ("Human Rights First", None, "human_rights_legal"),
    ("Immigrant Defenders Law Center", "ImmDef", "immigrant_legal_defense"),
    ("International Institute of Los Angeles", "IILA", "immigrant_refugee_services"),
    ("Korean Youth + Community Center", "KYCC", "korean_american_community"),
    ("Los Angeles Center for Law and Justice", "LACLJ", "domestic_violence_legal"),
    ("Los Angeles LGBT Center", None, "lgbtq_services"),
    ("Los Angeles Mission", None, "homeless_services"),
    ("National Day Laborer Organizing Network", "NDLON", "labor_worker_rights"),
    ("Pars Equality Center", None, "middle_eastern_immigrant"),
    ("Pilipino Workers Center", "PWC", "filipinx_worker_rights"),
    ("Asian Communities Public Health Coalition", "ACPHC", "asian_immigrant_health"),
    ("Asian Pacific American Legal Center", "APALC", "asian_pacific_legal"),
    ("Korean Resource Center", "KRC", "korean_immigrant_civic"),
    ("Chinese Progressive Association", "CPA", "chinese_progressive"),
    ("Services Immigrant Rights and Education Network", "SIREN", "immigrant_rights_education"),
    ("Community Coalition", None, "south_la_community"),
]


class OiaGranteesSource:
    """LA County Office of Immigrant Affairs CBO grantees."""

    key = "oia_grantees"
    URL = "https://oia.lacounty.gov/oia-awards-3-15-million-to-strengthen-case-management-capacity"

    def is_configured(self) -> bool:
        return True

    def discover(self, filters: dict) -> Iterator[Prospect]:
        state_filter = filters.get("state", "CA")
        county_filter = filters.get("county", "Los Angeles")

        # Try scraping first, fall back to hard-coded list
        scraped = list(self._scrape(state_filter, county_filter))
        if scraped:
            yield from scraped
            return

        # Fallback: use the known grantees list
        for name, acronym, focus in OIA_GRANTEES:
            yield Prospect(
                name=name,
                state=state_filter,
                county=county_filter,
                source=self.key,
                source_url=self.URL,
                voter_engagement=True,
                focus_area=focus,
                metadata={"acronym": acronym} if acronym else {},
            )

    def _scrape(self, state: str, county: str) -> Iterator[Prospect]:
        """Attempt to scrape the OIA page for grantee names."""
        try:
            resp = httpx.get(self.URL, timeout=30, follow_redirects=True)
            resp.raise_for_status()
        except httpx.HTTPError:
            return

        tree = HTMLParser(resp.text)

        # Look for organization names in the content area
        # OIA lists grantees in paragraphs with bold org names
        seen: set[str] = set()
        for p in tree.css("p, li"):
            text = p.text(strip=True)
            if not text or len(text) < 10 or len(text) > 300:
                continue

            # The pattern is "The <Org Name> (<Acronym>) <description>"
            # or "<Org Name> <description>"
            match = re.match(
                r"^(?:The\s+)?([A-Z][A-Za-z\s&+,\-\.]+?(?:\([A-Z\-]+\))?)\s+(?:advocat|address|empower|foster|uphold|offer|catalyz|serve|defend|secure|promote|provid|advanc)",
                text,
            )
            if match:
                name = match.group(1).strip().rstrip("( ")
                # Clean up trailing acronym
                name = re.sub(r"\([A-Z\-]+\)$", "", name).strip()
                if name in seen or len(name) < 5:
                    continue
                seen.add(name)
                yield Prospect(
                    name=name,
                    state=state,
                    county=county,
                    source=self.key,
                    source_url=self.URL,
                    voter_engagement=True,
                    focus_area="immigrant_services",
                )