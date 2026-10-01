"""
California Secretary of State — Promote the Vote / VCA Outreach partners.

Scrapes the SOS partner pages for organizations already doing voter
engagement. These are high-quality prospects — they already care about
civic participation.

Free public data — no API key needed.
"""

from __future__ import annotations

import re
from typing import Iterator

import httpx
from selectolax.parser import HTMLParser

from core.models import Prospect


# Known SOS partner organizations (from the Promote the Vote and VCA
# Outreach program pages). These are curated from the published partner
# spotlights and program descriptions — the SOS pages don't have a
# structured partner list, so we maintain a seed list and attempt to
# scrape for new additions.
SOS_PARTNERS = [
    "League of Women Voters of Sacramento County",
    "League of Women Voters of Los Angeles",
    "League of Women Voters of California",
    "Asian Law Caucus",
    "California Volunteers",
    "NALEO Educational Fund",
    "Mi Familia Vota",
    "Asian Americans Advancing Justice",
    "League of United Latin American Citizens",
    "Rock the Vote",
    "When We All Vote",
    "Common Cause California",
    "California Forward",
    "Democracy International",
]


class SosPartnersSource:
    """CA Secretary of State voter engagement partner organizations."""

    key = "sos_partners"

    URLS = [
        "https://www.sos.ca.gov/elections/promote-vote-ca/our-partners",
        "https://www.sos.ca.gov/voters-choice-act/vca-outreach-program",
    ]

    def is_configured(self) -> bool:
        return True

    def discover(self, filters: dict) -> Iterator[Prospect]:
        state_filter = filters.get("state", "CA")
        county_filter = filters.get("county")

        # The SOS pages don't have a structured partner list — they're
        # program description pages with navigation/section text that
        # looks like org names. Use the curated seed list instead.
        for name in SOS_PARTNERS:
            yield Prospect(
                name=name,
                state=state_filter,
                county=county_filter,
                source=self.key,
                source_url=self.URLS[0],
                voter_engagement=True,
                focus_area="civic_engagement",
            )

    def _scrape(self, state: str, county: str | None) -> list[Prospect]:
        """Attempt to extract org names from the SOS partner pages.

        Returns a list — if scraping produces only noise (fewer than 3
        real org names), the caller falls back to the seed list.
        """
        results: list[Prospect] = []
        seen: set[str] = set()

        with httpx.Client(timeout=30, follow_redirects=True) as client:
            for url in self.URLS:
                try:
                    resp = client.get(url)
                    resp.raise_for_status()
                except httpx.HTTPError:
                    continue

                tree = HTMLParser(resp.text)

                for node in tree.css("main a, article a, .content a"):
                    text = node.text(strip=True)
                    href = node.attributes.get("href", "")

                    if not text or len(text) < 5 or len(text) > 80:
                        continue
                    if text in seen:
                        continue
                    # Must link to an external site (org website)
                    if not href or href.startswith("#") or href.startswith("/"):
                        continue
                    # Must look like an org name — short, no sentences
                    if text.count(" ") > 5:
                        continue
                    # Skip emails, navigation, action text
                    skip_words = {
                        "@", "www.", ".gov", "learn more", "read more",
                        "sign up", "contact", "about", "home", "search",
                        "menu", "share", "email", "get started", "join",
                        "donate", "register", "vote", "election",
                        "ambassador", "webpage", "more days",
                    }
                    if any(w in text.lower() for w in skip_words):
                        continue

                    seen.add(text)
                    results.append(Prospect(
                        name=text,
                        state=state,
                        county=county,
                        source=self.key,
                        source_url=url,
                        voter_engagement=True,
                        focus_area="civic_engagement",
                        metadata={"website": href},
                    ))

        return results