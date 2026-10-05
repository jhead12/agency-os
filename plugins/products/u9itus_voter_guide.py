"""
u9itus voter guide — product plugin.

Knows how to describe the value of the u9itus voter guide to a prospect,
generate demo links, provision personal demo portals via the u9itus API,
pull events back, and provide pricing tiers for proposals.

generate_demo_link() makes NO HTTP calls — it returns the demo_link
stored on the outreach row, or falls back to a generic /compare URL.
Provisioning happens in provision_demo(), called by the CLI's
`provision` command, never during enqueue/dry-run.
"""

from __future__ import annotations

from typing import Optional
from urllib.parse import urlencode

from core.models import Prospect
from plugins.products.u9itus_client import U9itusClient


class U9itusVoterGuideProduct:
    """The u9itus digital voter guide platform."""

    key = "u9itus_voter_guide"
    BASE_URL = "https://www.u9itus.com"

    def __init__(self):
        self._client: Optional[U9itusClient] = None

    @property
    def client(self) -> U9itusClient:
        """Lazily create the API client."""
        if self._client is None:
            self._client = U9itusClient()
        return self._client

    def describe_value(self, prospect: Prospect) -> str:
        """One-line value prop personalized to this prospect."""
        focus = (prospect.focus_area or "civic engagement").replace("_", " ")
        return (
            f"a digital voter guide platform that lets {prospect.name} distribute "
            f"personalized, nonpartisan candidate comparisons and ballot measure "
            f"explanations to your constituents — aligned with your work in {focus}"
        )

    def generate_demo_link(self, prospect: Prospect, **kwargs) -> Optional[str]:
        """Return a demo link. Makes NO HTTP call.

        If the outreach row has a demo_link (provisioned portal), use it.
        Otherwise fall back to the generic /compare URL.

        This is called by Pipeline._build_variables() during enqueue,
        including dry runs, so it must never provision or hit the network.
        """
        # If a provisioned demo_link is passed via kwargs, use it
        demo_link = kwargs.get("demo_link")
        if demo_link:
            return demo_link

        # Fallback: generic comparison page (no API call)
        state = (prospect.state or "CA").lower()
        params = {"state": state}
        if kwargs.get("district"):
            params["district"] = kwargs["district"]
        return f"{self.BASE_URL}/compare?" + urlencode(params)

    # ── Demo portal provisioning (A2) ─────────────────────────────────

    def _to_org_type(self, prospect: Prospect) -> str:
        """Map IRS subsection (from prospect.metadata) to a u9itus org_type.

        The IRS BMF stores subsection as a 2-digit code ("03" = 501(c)(3)).
        Falls back to 'cbo' when unknown — cbo refuses candidate endorsements,
        which is the safe default. See doc/AGENCY_OS_INTEGRATION.md section 12
        ('Pre-seed org_type from IRS data').
        """
        import json
        try:
            metadata = json.loads(prospect.metadata) if isinstance(prospect.metadata, str) else (prospect.metadata or {})
        except (ValueError, TypeError):
            metadata = {}
        subsection = (metadata.get("irs_subsection") or "").lower().replace(" ", "").lstrip("0")
        if subsection in ("3", "501(c)(3)", "501c3", "c3"):
            return "c3_nonprofit"
        if subsection in ("4", "501(c)(4)", "501c4", "c4"):
            return "c4_nonprofit"
        if subsection in ("5", "501(c)(5)", "501c5", "c5"):
            return "union"
        return "cbo"

    def provision_demo(
        self,
        prospect: Prospect,
        contact_email: str = "",
        demo_link: str = "",
        refresh: bool = False,
    ) -> dict:
        """Provision a personal demo portal on u9itus for this prospect.

        Calls POST /api/v1/agency/demo-portals. Idempotent on external_ref.
        Returns dict with: slug, demo_url, claim_url, status, expires_at.
        Or {error: True, ...} on failure.
        """
        if not self.client.is_configured():
            return {"error": True, "detail": "U9ITUS_BASE_URL and U9ITUS_AGENCY_TOKEN not set"}

        external_ref = f"agency-os:prospect:{prospect.id}"

        import json
        try:
            metadata = json.loads(prospect.metadata) if isinstance(prospect.metadata, str) else (prospect.metadata or {})
        except (ValueError, TypeError):
            metadata = {}
        irs_subsection = metadata.get("irs_subsection") or ""

        return self.client.provision_demo(
            external_ref=external_ref,
            name=prospect.name,
            state=prospect.state or "CA",
            org_type=self._to_org_type(prospect),
            website_url=prospect.website_url or "",
            ein=prospect.ein or "",
            contact_email=contact_email,
            refresh=refresh,
            irs_subsection=irs_subsection,
        )

    def get_portal_status(self, prospect: Prospect) -> dict:
        """Check the status of a prospect's demo portal."""
        if not self.client.is_configured():
            return {"error": True, "detail": "U9itus API not configured"}

        external_ref = f"agency-os:prospect:{prospect.id}"
        return self.client.get_portal(external_ref)

    # ── Event pull (A2) ───────────────────────────────────────────────

    def pull_events(self, after: int = 0, limit: int = 100) -> dict:
        """Pull events from the u9itus event feed.

        Returns: {events: [...], next_cursor: int}
        Or {error: True, ...} on failure.
        """
        if not self.client.is_configured():
            return {"error": True, "detail": "U9itus API not configured"}

        return self.client.pull_events(after=after, limit=limit)

    def pricing_tiers(self) -> list[dict]:
        """Available pricing tiers for the voter guide product."""
        return [
            {
                "name": "Starter",
                "price": 500,
                "period": "per election cycle",
                "features": [
                    "Co-branded voter guide with your logo",
                    "Up to 1,000 constituents reached",
                    "Candidate comparisons side-by-side",
                    "Ballot measures in plain language",
                    "Email support",
                ],
            },
            {
                "name": "Pro",
                "price": 1500,
                "period": "per election cycle",
                "features": [
                    "Everything in Starter",
                    "Unlimited constituents",
                    "Multilingual support (Spanish, Korean, Chinese, Tagalog)",
                    "Embed the voter guide on your own website",
                    "Printable PDF voter guides",
                    "Priority support",
                ],
            },
            {
                "name": "Coalition",
                "price": 4000,
                "period": "per election cycle",
                "features": [
                    "Everything in Pro",
                    "Up to 10 partner CBOs under one umbrella",
                    "Custom branding per partner",
                    "Analytics dashboard — track engagement",
                    "Dedicated account manager",
                    "QR code generation for print materials",
                ],
            },
        ]