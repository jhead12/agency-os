"""
Firecrawl enricher: renders an org's website and has Firecrawl's LLM pull
out the contact person, email, phone and a short summary.

Handles JavaScript-heavy and bot-protected sites that local_scraper can't
read, so it works best listed after local_scraper as a fallback.

For each prospect:
  1. Use website_url, or find the site with Firecrawl search.
  2. Scrape the homepage with JSON extraction (and its links).
  3. If email or phone is still missing, scrape the best contact/about/team
     page linked from the homepage.

Requires FIRECRAWL_API_KEY. Costs credits: a JSON-extraction scrape is
5 credits a page, a search is 2 per 10 results, so up to ~12 per prospect.
"""

from __future__ import annotations

import os
import re
from typing import Optional
from urllib.parse import urlparse

import httpx

from core.models import Prospect, EnrichmentResult
from plugins.enrichers.local_scraper import (
    EMAIL_RE,
    JUNK_EMAIL_DOMAINS,
    JUNK_PHONE_PREFIXES,
    SUMMARY_MAX,
)

EXTRACT_PROMPT = (
    "Find the best person to contact at this organization (an owner, director, "
    "partner or other leader) with their job title, and the organization's main "
    "contact email address and phone number. Only return values that appear on "
    "the page. Also write one or two sentences on what the organization does."
)

EXTRACT_SCHEMA = {
    "type": "object",
    "properties": {
        "contact_name": {"type": "string"},
        "contact_title": {"type": "string"},
        "contact_email": {"type": "string"},
        "contact_phone": {"type": "string"},
        "summary": {"type": "string"},
    },
}

# Link paths worth a second scrape, best first
CONTACT_LINK_HINTS = ("contact", "staff", "team", "leadership", "attorneys", "people", "about")

# Search results that are directories or social profiles, not the org's own site
SKIP_DOMAINS = (
    "facebook.com", "twitter.com", "x.com", "instagram.com", "linkedin.com",
    "youtube.com", "yelp.com", "wikipedia.org", "guidestar.org",
    "charitynavigator.org", "propublica.org", "irs.gov", "google.com",
    "bbb.org", "yellowpages.com", "mapquest.com", "healthgrades.com",
    "zocdoc.com", "avvo.com", "justia.com", "martindale.com", "findlaw.com",
)


class FirecrawlEnricher:
    """Firecrawl scrape + LLM extraction of an org's contact info."""

    key = "firecrawl"
    API_BASE = "https://api.firecrawl.dev/v2"

    def __init__(self):
        self._api_key = os.environ.get("FIRECRAWL_API_KEY", "")
        self._key_valid = None  # cache: None=unknown, True/False after one probe
        self._client = httpx.Client(
            timeout=60,
            headers={"Authorization": f"Bearer {self._api_key}"},
        )

    def is_configured(self) -> bool:
        """True only when a key is set and works. Probes the API once and caches."""
        if not self._api_key:
            return False
        if self._key_valid is not None:
            return self._key_valid
        try:
            resp = self._client.get(f"{self.API_BASE}/team/credit-usage", timeout=10)
            self._key_valid = resp.status_code == 200
        except Exception:
            self._key_valid = False
        return self._key_valid

    def enrich(self, prospect: Prospect) -> EnrichmentResult:
        """Find the website, extract contact info from it."""
        result = EnrichmentResult(source=self.key, confidence=0.5)
        if not self.is_configured():
            return result
        try:
            website = self._find_website(prospect)
            if not website:
                return result
            result.raw["website"] = website

            found, links = self._scrape(website, with_links=True)
            if not (found.get("contact_email") and found.get("contact_phone")):
                page = self._contact_page(website, links)
                if page:
                    more, _ = self._scrape(page)
                    for field, value in more.items():
                        found.setdefault(field, value)

            self._apply(result, found, website)
            return result
        except Exception:
            return result

    # ── Firecrawl calls ──────────────────────────────────────────────

    def _find_website(self, prospect: Prospect) -> Optional[str]:
        if prospect.website_url:
            url = prospect.website_url.strip()
            return url if url.startswith("http") else "https://" + url

        query = " ".join(p for p in (prospect.name, prospect.city, prospect.state) if p)
        resp = self._client.post(f"{self.API_BASE}/search", json={"query": query, "limit": 5})
        resp.raise_for_status()
        data = resp.json().get("data") or {}
        hits = data.get("web", []) if isinstance(data, dict) else data
        for hit in hits:
            url = hit.get("url", "")
            domain = urlparse(url).netloc.lower()
            if url.startswith("http") and not any(d in domain for d in SKIP_DOMAINS):
                return url
        return None

    def _scrape(self, url: str, with_links: bool = False) -> tuple[dict, list[str]]:
        """Scrape one page. Returns (non-empty extracted fields, links on the page)."""
        formats: list = [{"type": "json", "prompt": EXTRACT_PROMPT, "schema": EXTRACT_SCHEMA}]
        if with_links:
            formats.append("links")
        resp = self._client.post(
            f"{self.API_BASE}/scrape",
            json={"url": url, "formats": formats, "onlyMainContent": False},
        )
        resp.raise_for_status()
        data = resp.json().get("data") or {}
        extracted = data.get("json") or {}
        fields = {k: v.strip() for k, v in extracted.items() if isinstance(v, str) and v.strip()}
        return fields, data.get("links") or []

    def _contact_page(self, website: str, links: list[str]) -> Optional[str]:
        """The best same-site contact/staff/about link, if any."""
        site = urlparse(website).netloc.lower().removeprefix("www.")
        same_site = [
            l for l in links
            if urlparse(l).netloc.lower().removeprefix("www.") == site
            and l.rstrip("/") != website.rstrip("/")
        ]
        for hint in CONTACT_LINK_HINTS:
            for link in same_site:
                if hint in urlparse(link).path.lower():
                    return link
        return None

    # ── Cleanup ──────────────────────────────────────────────────────

    def _apply(self, result: EnrichmentResult, found: dict, website: str) -> None:
        phone = self._normalize_phone(found.get("contact_phone", ""))
        if phone:
            result.contact_phone = phone
            result.confidence = min(result.confidence + 0.2, 1.0)

        email = self._clean_email(found.get("contact_email", ""))
        if email:
            result.contact_email = email
            result.confidence = min(result.confidence + 0.2, 1.0)

        name = found.get("contact_name")
        if name and 5 <= len(name) <= 50:
            result.contact_name = name
            result.contact_title = found.get("contact_title")
            result.confidence = min(result.confidence + 0.1, 1.0)

        summary = found.get("summary")
        if summary:
            result.raw["site_summary"] = summary[:SUMMARY_MAX]
        result.raw["firecrawl"] = found

    def _clean_email(self, raw: str) -> Optional[str]:
        email = raw.strip().lower().removeprefix("mailto:")
        if not EMAIL_RE.fullmatch(email):
            return None
        if email.partition("@")[2] in JUNK_EMAIL_DOMAINS:
            return None
        return email

    def _normalize_phone(self, raw: str) -> Optional[str]:
        """Normalize to (XXX) XXX-XXXX format, dropping toll-free/junk numbers."""
        digits = re.sub(r"[^\d]", "", raw)
        if len(digits) == 11 and digits.startswith("1"):
            digits = digits[1:]
        if len(digits) != 10:
            return None
        area, prefix, line = digits[:3], digits[3:6], digits[6:]
        if area in JUNK_PHONE_PREFIXES or prefix in JUNK_PHONE_PREFIXES:
            return None
        return f"({area}) {prefix}-{line}"

    def __del__(self):
        try:
            self._client.close()
        except Exception:
            pass
