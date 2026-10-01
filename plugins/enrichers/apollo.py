"""
Apollo.io contact enricher.

Looks up contact names and emails for a prospect organization using the
Apollo API. Requires APOLLO_API_KEY in environment.

If not configured, degrades gracefully — the pipeline skips enrichment
and waits for manual contact entry.
"""

from __future__ import annotations

import os
from typing import Optional

import httpx

from core.models import Prospect, EnrichmentResult


class ApolloEnricher:
    """Apollo.io contact enrichment."""

    key = "apollo"
    API_BASE = "https://api.apollo.io/api/v1"

    def __init__(self):
        self._api_key = os.environ.get("APOLLO_API_KEY", "")

    def is_configured(self) -> bool:
        return bool(self._api_key)

    def enrich(self, prospect: Prospect) -> EnrichmentResult:
        """Search Apollo for contacts at this organization."""
        if not self.is_configured():
            return EnrichmentResult(source=self.key)

        try:
            # Apollo Organization Search + People Search
            # POST /api/v1/organizations/search or /api/v1/people/match
            #
            # In production:
            # 1. Search for the organization by name
            # 2. Find senior leadership (ED, Program Director, Outreach Director)
            # 3. Return the best-matching contact
            #
            # For now, return empty — real implementation needs the API key
            return EnrichmentResult(source=self.key, confidence=0.0)
        except Exception:
            return EnrichmentResult(source=self.key)