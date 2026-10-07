"""
Apollo.io contact enricher — real implementation.

Flow:
  1. Organization Search → find the org by name, get its Apollo ID + domain
  2. People API Search → find senior leadership (ED, Director, etc.) at that org
  3. Bulk People Enrichment → retrieve email addresses for found people

Auth: x-api-key header
Base URL: https://api.apollo.io/api/v1

NOTE: Apollo's FREE plan does NOT include API access. All search and
enrichment endpoints return 403 API_INACCESSIBLE on free plans. A paid
plan (Basic $49/mo+) is required for any API calls to work.

If you're on a free plan, this enricher detects the 403 and skips
gracefully — the local_scraper enricher handles contact discovery
without any API keys.
"""

from __future__ import annotations

import os
import time
from typing import Optional
from urllib.parse import urlparse

import httpx

from core.models import Prospect, EnrichmentResult


# Titles we want to find at nonprofit CBOs (in priority order)
TARGET_TITLES = [
    "executive director",
    "program director",
    "outreach director",
    "development director",
    "deputy director",
    "communications director",
    "program manager",
    "office manager",
    "coordinator",
    "founder",
    "ceo",
    "president",
    "executive",
]

# Seniority levels to search for
TARGET_SENIORITIES = ["c_suite", "owner", "founder", "head", "director", "manager"]


class ApolloEnricher:
    """Apollo.io contact enrichment — real implementation."""

    key = "apollo"
    API_BASE = "https://api.apollo.io/api/v1"

    def __init__(self):
        self._api_key = os.environ.get("APOLLO_API_KEY", "").strip().strip('"').strip("'")
        self._client = httpx.Client(
            timeout=20,
            headers={
                "x-api-key": self._api_key,
                "Content-Type": "application/json",
                "Cache-Control": "no-cache",
            },
        )
        self._plan_accessible = None  # cache: None=unknown, True=ok, False=403

    def is_configured(self) -> bool:
        """True only when a key is set and the plan allows API access.

        Probes once (cached) so the pipeline can skip Apollo entirely on a
        free plan instead of trying it for every prospect.
        """
        if not self._api_key:
            return False
        return self._check_access()

    def _check_access(self) -> bool:
        """Quick check if the API plan allows access. Caches the result."""
        if self._plan_accessible is not None:
            return self._plan_accessible
        if not self._api_key:
            self._plan_accessible = False
            return False
        try:
            resp = self._client.post(
                f"{self.API_BASE}/mixed_companies/search",
                params={"q_organization_name": "test"},
            )
            if resp.status_code == 403:
                print("  ! Apollo API: Free plan detected — API endpoints not accessible.")
                print("    Apollo requires a paid plan ($49/mo+) for API access.")
                print("    local_scraper will handle contact discovery instead.")
                self._plan_accessible = False
                return False
            self._plan_accessible = True
            return True
        except Exception:
            self._plan_accessible = False
            return False

    def enrich(self, prospect: Prospect) -> EnrichmentResult:
        """Find contact info for a prospect via Apollo."""
        if not self.is_configured():
            return EnrichmentResult(source=self.key)

        if not self._check_access():
            return EnrichmentResult(source=self.key)

        result = EnrichmentResult(source=self.key, confidence=0.5)

        # Step 1: Find the organization in Apollo
        org_info = self._find_organization(prospect)
        if not org_info:
            return result

        org_id = org_info.get("id")
        org_domain = org_info.get("primary_domain", "")
        result.raw["apollo_org_id"] = org_id
        result.raw["apollo_org_domain"] = org_domain
        if org_domain:
            result.raw["website"] = f"https://{org_domain}"

        # Step 2: Search for people at this org
        people = self._search_people(org_id, org_domain, prospect)
        if not people:
            return result

        # Step 3: Pick the best candidate and enrich to get email
        best_person = self._pick_best_person(people)
        if not best_person:
            return result

        # The search endpoint may already have email if the person is a saved contact
        email = best_person.get("email")
        if not email and best_person.get("id"):
            # Use bulk enrichment to get the email (costs credits)
            enriched = self._enrich_person(best_person["id"])
            if enriched:
                email = enriched.get("email")
                if enriched.get("phone") and not result.contact_phone:
                    result.contact_phone = enriched.get("phone")
                if enriched.get("organization", {}).get("primary_domain") and not result.raw.get("website"):
                    domain = enriched["organization"]["primary_domain"]
                    result.raw["website"] = f"https://{domain}"

        person_name = f"{best_person.get('first_name', '')} {best_person.get('last_name', '')}".strip()
        if not person_name or "@" in person_name:
            person_name = best_person.get("name", "")

        result.contact_name = person_name or None
        result.contact_email = email or None
        result.contact_title = best_person.get("title") or None

        if email:
            result.confidence = 0.9
        elif person_name:
            result.confidence = 0.7
        else:
            result.confidence = 0.3

        result.raw["apollo_person_id"] = best_person.get("id")

        return result

    # ── Step 1: Organization Search ───────────────────────────────────

    def _find_organization(self, prospect: Prospect) -> Optional[dict]:
        """Search Apollo for the organization by name or domain."""
        # If we already have a website, extract the domain and search by it
        if prospect.website_url:
            parsed = urlparse(prospect.website_url)
            domain = parsed.netloc.replace("www.", "")
            if domain:
                org = self._search_org_by_domain(domain)
                if org:
                    return org

        # Search by name
        return self._search_org_by_name(prospect.name)

    def _search_org_by_domain(self, domain: str) -> Optional[dict]:
        """Find an org by its website domain."""
        try:
            resp = self._client.post(
                f"{self.API_BASE}/mixed_companies/search",
                params={"q_organization_domains_list[]": [domain]},
            )
            if resp.status_code != 200:
                return None
            data = resp.json()
            organizations = data.get("organizations", [])
            if organizations:
                org = organizations[0]
                return {
                    "id": org.get("id"),
                    "name": org.get("name"),
                    "primary_domain": org.get("primary_domain", domain),
                }
            return None
        except Exception:
            return None

    def _search_org_by_name(self, name: str) -> Optional[dict]:
        """Find an org by its name (partial match)."""
        try:
            resp = self._client.post(
                f"{self.API_BASE}/mixed_companies/search",
                params={"q_organization_name": name},
            )
            if resp.status_code != 200:
                return None
            data = resp.json()
            organizations = data.get("organizations", [])
            if not organizations:
                return None

            # Find the best match (exact or closest name match)
            name_lower = name.lower()
            for org in organizations:
                org_name = (org.get("name") or "").lower()
                if org_name == name_lower:
                    return {
                        "id": org.get("id"),
                        "name": org.get("name"),
                        "primary_domain": org.get("primary_domain", ""),
                    }

            # Return the first result if no exact match
            org = organizations[0]
            return {
                "id": org.get("id"),
                "name": org.get("name"),
                "primary_domain": org.get("primary_domain", ""),
            }
        except Exception:
            return None

    # ── Step 2: People Search ─────────────────────────────────────────

    def _search_people(self, org_id: str, org_domain: str, prospect: Prospect) -> list[dict]:
        """Search for senior staff at the organization."""
        # Strategy 1: Search by org ID + seniority
        if org_id:
            people = self._search_people_by_org_id(org_id)
            if people:
                return people

        # Strategy 2: Search by domain + seniority
        if org_domain and not people:
            people = self._search_people_by_domain(org_domain)
            if people:
                return people

        # Strategy 3: Search by org name keyword + location
        if not people:
            people = self._search_people_by_name(prospect)

        return people

    def _search_people_by_org_id(self, org_id: str) -> list[dict]:
        """Search for people at an org by Apollo org ID."""
        try:
            params = {
                "organization_ids[]": [org_id],
                "person_seniorities[]": TARGET_SENIORITIES,
                "per_page": 10,
            }
            resp = self._client.post(
                f"{self.API_BASE}/mixed_people/api_search",
                params=params,
            )
            if resp.status_code != 200:
                return []
            data = resp.json()
            return data.get("people", [])
        except Exception:
            return []

    def _search_people_by_domain(self, domain: str) -> list[dict]:
        """Search for people at an org by domain."""
        try:
            params = {
                "q_organization_domains_list[]": [domain],
                "person_seniorities[]": TARGET_SENIORITIES,
                "per_page": 10,
            }
            resp = self._client.post(
                f"{self.API_BASE}/mixed_people/api_search",
                params=params,
            )
            if resp.status_code != 200:
                return []
            data = resp.json()
            return data.get("people", [])
        except Exception:
            return []

    def _search_people_by_name(self, prospect: Prospect) -> list[dict]:
        """Search for people by org name keyword + location (fallback)."""
        try:
            name_parts = prospect.name.split()[:3]
            org_keyword = " ".join(name_parts)

            params = {
                "q_organization_keyword_tags[]": [org_keyword],
                "organization_locations[]": [prospect.state or "California"],
                "person_seniorities[]": TARGET_SENIORITIES,
                "per_page": 5,
            }
            resp = self._client.post(
                f"{self.API_BASE}/mixed_people/api_search",
                params=params,
            )
            if resp.status_code != 200:
                return []
            data = resp.json()
            return data.get("people", [])
        except Exception:
            return []

    # ── Step 3: People Enrichment ─────────────────────────────────────

    def _enrich_person(self, person_id: str) -> Optional[dict]:
        """Enrich a person to get their email (costs credits)."""
        try:
            resp = self._client.post(
                f"{self.API_BASE}/people/bulk_match",
                json={"details": [{"id": person_id}]},
            )
            if resp.status_code != 200:
                return None
            data = resp.json()
            matches = data.get("matches", [])
            if matches:
                return matches[0]
            return None
        except Exception:
            return None

    # ── Helpers ───────────────────────────────────────────────────────

    def _pick_best_person(self, people: list[dict]) -> Optional[dict]:
        """Pick the most relevant person from search results."""
        if not people:
            return None

        # Score each person by title relevance
        scored = []
        for person in people:
            title = (person.get("title") or "").lower()
            score = 0
            for i, target in enumerate(TARGET_TITLES):
                if target in title:
                    score = len(TARGET_TITLES) - i
                    break
            # Boost if they already have an email (saved contact)
            if person.get("email"):
                score += 100
            scored.append((score, person))

        scored.sort(key=lambda x: x[0], reverse=True)
        return scored[0][1] if scored else None

    def __del__(self):
        try:
            self._client.close()
        except Exception:
            pass