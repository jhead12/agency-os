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

import secrets
import time
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urlencode

from core.models import Prospect
from core.protocols import EventEffect, shared_event_effect
from plugins.products import u9itus_client
from plugins.products.u9itus_client import U9itusClient


class U9itusVoterGuideProduct:
    """The u9itus digital voter guide platform."""

    key = "u9itus_voter_guide"
    BASE_URL = "https://www.u9itus.com"

    # DemoPortalProduct (core/protocols.py). "u9itus" is also what existing
    # prospect metadata and activity-log refs use, so it must not change.
    portal_namespace = "u9itus"
    portal_label = "u9itus demo page"
    # Every u9itus product reads the same event feed; this keeps the cursor
    # this product has always used (core/protocols.py event_feed).
    event_feed = "u9itus_voter_guide"

    # Plain-language reasons for the agency API's error statuses.
    ERROR_HINTS = {401: "token rejected", 404: "agency API not deployed at this URL",
                   503: "AGENCY_OS_TOKEN_HASH not set on u9itus"}

    def __init__(self):
        self._client: Optional[U9itusClient] = None

    @property
    def client(self) -> U9itusClient:
        """Lazily create the API client."""
        if self._client is None:
            self._client = u9itus_client.U9itusClient()
        return self._client

    def is_configured(self) -> bool:
        return self.client.is_configured()

    def setup_hint(self) -> str:
        return "Set U9ITUS_BASE_URL and U9ITUS_AGENCY_TOKEN in .env"

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
        Churches (IRS foundation code 10, or a religious-congregation NTEE code)
        map to 'church'. Falls back to 'cbo' when unknown — cbo refuses candidate endorsements,
        which is the safe default. See doc/AGENCY_OS_INTEGRATION.md section 12
        ('Pre-seed org_type from IRS data').
        """
        import json
        try:
            metadata = json.loads(prospect.metadata) if isinstance(prospect.metadata, str) else (prospect.metadata or {})
        except (ValueError, TypeError):
            metadata = {}
        subsection = (metadata.get("irs_subsection") or "").lower().replace(" ", "").lstrip("0")
        if self.is_church(prospect, metadata):
            return "church"
        if subsection in ("3", "501(c)(3)", "501c3", "c3"):
            return "c3_nonprofit"
        if subsection in ("4", "501(c)(4)", "501c4", "c4"):
            return "c4_nonprofit"
        if subsection in ("5", "501(c)(5)", "501c5", "c5"):
            return "union"
        return "cbo"

    # NTEE X20–X70: congregations by faith (Christian, Jewish, Islamic,
    # Buddhist, Hindu, other). X80/X90 are religious media and interfaith groups.
    CHURCH_NTEE_PREFIXES = ("X2", "X3", "X4", "X5", "X6", "X7")

    @classmethod
    def is_church(cls, prospect: Prospect, metadata: Optional[dict] = None) -> bool:
        """Whether the IRS data says this prospect is a church or other house of worship."""
        metadata = metadata if metadata is not None else (prospect.metadata if isinstance(prospect.metadata, dict) else {})
        if str(metadata.get("irs_foundation") or "").zfill(2) == "10":
            return True
        ntee = (metadata.get("ntee_full") or prospect.ntee_code or "").upper()
        return ntee.startswith(cls.CHURCH_NTEE_PREFIXES)

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

    # ── Plugins-page test buttons ─────────────────────────────────────

    def check_connection(self) -> dict:
        """Read one event (no side effects) to confirm the URL and token."""
        return self._with_fresh_client(lambda client: client.pull_events(after=0, limit=1))

    def create_test_portal(self) -> dict:
        """Make a blank demo portal with a throwaway external_ref; it expires on
        its own after 60 days like any unclaimed demo."""
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S") + "-" + secrets.token_hex(2)
        return self._with_fresh_client(lambda client: client.provision_demo(
            external_ref=f"agency-os:test:{stamp}", name=f"Test Portal {stamp}", state="CA",
        ))

    def _with_fresh_client(self, call) -> dict:
        client = u9itus_client.U9itusClient()
        if not client.is_configured():
            return {"error": True, "detail": self.setup_hint()}
        try:
            result = call(client)
        finally:
            client.close()
        if result.get("error") and result.get("status") in self.ERROR_HINTS:
            result = {**result, "detail": self.ERROR_HINTS[result["status"]]}
        return result

    # ── Pricing (u9itus is the source of truth for prices) ───────────

    # Sales copy per plan. Prices and plan names come from u9itus
    # (GET /api/v1/agency/plans); FALLBACK_PRICES is used only when u9itus
    # can't be reached, so a proposal never shows no price at all.
    PLAN_FEATURES = {
        "starter": [
            "Co-branded voter guide with your logo",
            "Up to 1,000 constituents reached",
            "Candidate comparisons side-by-side",
            "Ballot measures in plain language",
            "Email support",
        ],
        "pro": [
            "Everything in Starter",
            "Unlimited constituents",
            "Multilingual support (Spanish, Korean, Chinese, Tagalog)",
            "Embed the voter guide on your own website",
            "Printable PDF voter guides",
            "Priority support",
        ],
        "coalition": [
            "Everything in Pro",
            "Up to 10 partner CBOs under one umbrella",
            "Custom branding per partner",
            "Analytics dashboard — track engagement",
            "Dedicated account manager",
            "QR code generation for print materials",
        ],
    }
    FALLBACK_PRICES = [("starter", "Starter", 50000), ("pro", "Pro", 150000), ("coalition", "Coalition", 400000)]
    PLANS_TTL_SECONDS = 600

    _plans_cache: Optional[tuple[float, list[tuple[str, str, int]]]] = None

    def _plans(self) -> list[tuple[str, str, int]]:
        """(key, label, amount_cents) per plan, from u9itus, cached for 10 minutes."""
        now = time.monotonic()
        if self._plans_cache and now - self._plans_cache[0] < self.PLANS_TTL_SECONDS:
            return self._plans_cache[1]
        plans = self.FALLBACK_PRICES
        if self.client.is_configured():
            result = self.client.get_plans()
            fetched = [(p["key"], p["label"], int(p["amount_cents"]))
                       for p in result.get("plans", []) if p.get("key") and "amount_cents" in p]
            if fetched and not result.get("error"):
                plans = fetched
                self._plans_cache = (now, plans)
        return plans

    def pricing_tiers(self) -> list[dict]:
        """Available pricing tiers for the voter guide product."""
        return [
            {
                "key": key,
                "name": label,
                "price": cents / 100 if cents % 100 else cents // 100,
                "period": "per election cycle",
                "features": self.PLAN_FEATURES.get(key, []),
            }
            for key, label, cents in self._plans()
        ]

    # ── Event effects (core/protocols.py) ─────────────────────────────

    def event_effect(self, event: dict) -> Optional[EventEffect]:
        """The shared portal.* events, plus u9itus's subscription.* events.

        A paid plan flags the deal ready to close with the plan and amount on
        the activity entry; the rep moves it to closed_won. Expiry and
        cancellation are only noted: they never move a stage, backward or
        out of closed_won/closed_lost.
        """
        data = event.get("data") or {}
        event_type = event.get("type")
        if event_type == "subscription.activated":
            return EventEffect(
                log_as=event_type, flag="ready_to_close",
                detail={k: data[k] for k in ("plan", "amount_cents", "cycle") if k in data},
                portal={"subscription_status": "active", "plan": data.get("plan"), "cycle": data.get("cycle")},
            )
        if event_type in ("subscription.expired", "subscription.canceled"):
            return EventEffect(
                log_as=event_type,
                detail={"cycle": data["cycle"]} if "cycle" in data else {},
                portal={"subscription_status": event_type.split(".")[1]},
            )
        return shared_event_effect(event)
