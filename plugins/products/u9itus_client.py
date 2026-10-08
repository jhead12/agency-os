"""
U9itus API client — HTTP client for the u9itus agency integration.

Handles:
  - POST /api/v1/agency/demo-portals  (provision a demo portal)
  - GET  /api/v1/agency/demo-portals/{external_ref}  (check portal status)
  - GET  /api/v1/agency/events?after={cursor}  (pull event feed)

Auth: Bearer token (U9ITUS_AGENCY_TOKEN in .env)
Base URL: U9ITUS_BASE_URL in .env (e.g. https://www.u9itus.com)

Never logs the token. Retries on 5xx with exponential backoff.
10-second timeout per request.
"""

from __future__ import annotations

import os
import time
from typing import Optional

import httpx


class U9itusClient:
    """Thin HTTP client for the u9itus agency API."""

    def __init__(
        self,
        base_url: str = "",
        token: str = "",
        timeout: int = 10,
        max_retries: int = 3,
    ):
        self._base_url = (base_url or os.environ.get("U9ITUS_BASE_URL", "")).rstrip("/")
        self._token = (token or os.environ.get("U9ITUS_AGENCY_TOKEN", "")).strip().strip('"').strip("'")
        self._timeout = timeout
        self._max_retries = max_retries
        self._client: Optional[httpx.Client] = None

    @property
    def client(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(
                timeout=self._timeout,
                headers={
                    "Authorization": f"Bearer {self._token}",
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                },
            )
        return self._client

    def is_configured(self) -> bool:
        """Whether both base URL and token are set."""
        return bool(self._base_url and self._token)

    # ── Provision a demo portal ───────────────────────────────────────

    def provision_demo(
        self,
        external_ref: str,
        name: str,
        state: str,
        org_type: str = "cbo",
        district: str = "",
        website_url: str = "",
        ein: str = "",
        contact_email: str = "",
        refresh: bool = False,
        irs_subsection: str = "",
    ) -> dict:
        """POST /api/v1/agency/demo-portals — create or return a demo portal.

        Idempotent on external_ref: repeat calls return 200 with the existing portal.
        Returns dict with: slug, demo_url, claim_url, status, expires_at.
        """
        payload = {
            "external_ref": external_ref,
            "name": name,
            "org_type": org_type,
            "state": state,
        }
        if district:
            payload["district"] = district
        if website_url:
            payload["website_url"] = website_url
        if ein:
            payload["ein"] = ein
        if contact_email:
            payload["contact_email"] = contact_email
        if refresh:
            payload["refresh"] = True
        if irs_subsection:
            payload["irs_subsection"] = irs_subsection

        resp = self._request("POST", "/api/v1/agency/demo-portals", json=payload)
        return resp

    # ── Get portal status ─────────────────────────────────────────────

    def get_portal(self, external_ref: str) -> dict:
        """GET /api/v1/agency/demo-portals/{external_ref} — check portal status.

        Returns same shape as provision_demo, plus traffic: {views_30d, last_viewed_on}.
        """
        return self._request("GET", f"/api/v1/agency/demo-portals/{external_ref}")

    # ── Pull events ───────────────────────────────────────────────────

    def pull_events(self, after: int = 0, limit: int = 100) -> dict:
        """GET /api/v1/agency/events?after={cursor}&limit={limit}.

        Returns: {events: [...], next_cursor: int}
        """
        return self._request(
            "GET",
            "/api/v1/agency/events",
            params={"after": after, "limit": limit},
        )

    # ── Plans ─────────────────────────────────────────────────────────

    def get_plans(self) -> dict:
        """GET /api/v1/agency/plans — the prices u9itus charges.

        Returns: {currency, cycle, cycle_ends_at, plans: [{key, label, amount_cents}]}
        """
        return self._request("GET", "/api/v1/agency/plans")

    # ── Internal request with retry ───────────────────────────────────

    def _request(self, method: str, path: str, **kwargs) -> dict:
        """Make an HTTP request with retry on 5xx. Never logs the token."""
        url = f"{self._base_url}{path}"

        for attempt in range(self._max_retries):
            try:
                resp = self.client.request(method, url, **kwargs)

                # Success
                if resp.status_code < 300:
                    return resp.json()

                # Client errors — don't retry
                if resp.status_code < 500:
                    error_detail = resp.text[:500]
                    return {
                        "error": True,
                        "status": resp.status_code,
                        "detail": error_detail,
                    }

                # 5xx — retry with backoff
                if attempt < self._max_retries - 1:
                    wait = 2 ** attempt  # 1s, 2s, 4s
                    time.sleep(wait)
                    continue

                # Max retries exhausted
                return {
                    "error": True,
                    "status": resp.status_code,
                    "detail": f"Server error after {self._max_retries} retries",
                }

            except httpx.TimeoutException:
                if attempt < self._max_retries - 1:
                    time.sleep(2 ** attempt)
                    continue
                return {"error": True, "status": 0, "detail": "Request timed out"}

            except httpx.ConnectError:
                if attempt < self._max_retries - 1:
                    time.sleep(2 ** attempt)
                    continue
                return {"error": True, "status": 0, "detail": "Connection failed"}

            except Exception as exc:
                return {"error": True, "status": 0, "detail": str(exc)}

        return {"error": True, "status": 0, "detail": "Max retries exceeded"}

    def close(self):
        if self._client:
            self._client.close()
            self._client = None

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass