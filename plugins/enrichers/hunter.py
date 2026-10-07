"""
Hunter.io email finder + verifier.

Finds and verifies email addresses for a prospect organization using
the Hunter API. Requires HUNTER_API_KEY in environment.
"""

from __future__ import annotations

import os

import httpx

from core.models import Prospect, EnrichmentResult


class HunterEnricher:
    """Hunter.io domain email search + verification."""

    key = "hunter"
    API_BASE = "https://api.hunter.io/v2"

    def __init__(self):
        self._api_key = os.environ.get("HUNTER_API_KEY", "")
        self._key_valid = None  # cache: None=unknown, True/False after one probe

    def is_configured(self) -> bool:
        """True only when a key is set and works. Probes the API once and caches."""
        if not self._api_key:
            return False
        if self._key_valid is not None:
            return self._key_valid
        try:
            resp = httpx.get(
                f"{self.API_BASE}/account",
                params={"api_key": self._api_key},
                timeout=10,
            )
            self._key_valid = resp.status_code == 200
        except Exception:
            self._key_valid = False
        return self._key_valid

    def enrich(self, prospect: Prospect) -> EnrichmentResult:
        """Search Hunter for email patterns at this org's domain."""
        if not self.is_configured():
            return EnrichmentResult(source=self.key)

        # Need a website/domain to look up
        if not prospect.website_url:
            return EnrichmentResult(source=self.key)

        try:
            # Extract domain from website_url
            from urllib.parse import urlparse

            domain = urlparse(prospect.website_url).netloc
            if not domain:
                return EnrichmentResult(source=self.key)

            # GET /v2/email-finder?domain=...&api_key=...
            resp = httpx.get(
                f"{self.API_BASE}/email-finder",
                params={"domain": domain, "api_key": self._api_key, "limit": 1},
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json().get("data", {})

            if data.get("email"):
                return EnrichmentResult(
                    contact_name=f"{data.get('first_name', '')} {data.get('last_name', '')}".strip() or None,
                    contact_email=data["email"],
                    contact_title=data.get("position"),
                    confidence=float(data.get("score", 0)) / 100.0,
                    source=self.key,
                    raw=data,
                )
            return EnrichmentResult(source=self.key)
        except Exception:
            return EnrichmentResult(source=self.key)