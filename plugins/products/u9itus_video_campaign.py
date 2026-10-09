"""
u9itus video campaign — product plugin.

Sells sponsored video campaigns on u9itus to CBOs and churches: residents near
the organization are paid a small amount to watch its video, and the
organization sees who was new to it and who clicked to donate, volunteer or
visit. See u9itus doc/AGENCY_OS_INTEGRATION.md section 13.

It provisions the same personal demo page as the voter guide product, but
sends the prospect's own video (found by the local_scraper enricher) and puts
the sandboxed video demo link in the emails instead of the voter guide link.
Provisioning still happens only in provision_demo(), never in
generate_demo_link() or a dry run.
"""

from __future__ import annotations

import json
import time
from typing import Optional

from core.models import Prospect
from plugins.products.u9itus_voter_guide import U9itusVoterGuideProduct


class U9itusVideoCampaignProduct(U9itusVoterGuideProduct):
    """Sponsored, pay-per-view video campaigns on u9itus."""

    key = "u9itus_video_campaign"
    portal_label = "u9itus video demo"

    # View packs a proposal offers. Sizes are a sales choice; the per-view
    # price always comes from u9itus (GET /api/v1/agency/plans → video).
    VIEW_PACKS = [("reach_500", "Neighborhood", 500), ("reach_2000", "Community", 2000), ("reach_5000", "Citywide", 5000)]
    FALLBACK_PER_VIEW_CENTS = 75
    PACK_FEATURES = [
        "Your video shown to residents near you, who are paid to watch it",
        "A button after the video: donate, volunteer, visit, or any link",
        "Viewers can ask you questions; you reply from your dashboard",
        "One survey question, e.g. \"Did you know about us before today?\"",
        "Weekly report email: views, clicks, questions and answers",
    ]

    _rate_cache: Optional[tuple[float, int]] = None

    @staticmethod
    def _metadata(prospect: Prospect) -> dict:
        try:
            return json.loads(prospect.metadata) if isinstance(prospect.metadata, str) else (prospect.metadata or {})
        except (ValueError, TypeError):
            return {}

    def describe_value(self, prospect: Prospect) -> str:
        audience = "your congregation's neighbors" if self.is_church(prospect, self._metadata(prospect)) else "people in your community"
        return (
            f"a sponsored video campaign that puts {prospect.name}'s message in front of "
            f"{audience} who don't know you yet. Residents are paid a small amount to watch, "
            f"and you see who was new to you and who clicked to donate, volunteer or visit"
        )

    def generate_demo_link(self, prospect: Prospect, **kwargs) -> Optional[str]:
        """The provisioned video demo link. Makes NO HTTP call. Before
        provisioning there is no personal demo, so the email links to the
        u9itus page that explains the idea rather than leaving the
        {{demo_link}} placeholder in the text."""
        return kwargs.get("demo_link") or f"{self.BASE_URL}/about"

    def provision_demo(
        self,
        prospect: Prospect,
        contact_email: str = "",
        demo_link: str = "",
        refresh: bool = False,
    ) -> dict:
        """Provision the demo page with the prospect's own video, and hand the
        pipeline the video demo link as `demo_url` (the voter guide link is
        kept as `portal_demo_url`)."""
        if not self.client.is_configured():
            return {"error": True, "detail": "U9ITUS_BASE_URL and U9ITUS_AGENCY_TOKEN not set"}

        metadata = self._metadata(prospect)
        youtube = metadata.get("youtube_url") or ""
        is_channel = "/@" in youtube or "/channel/" in youtube or "/c/" in youtube or "/user/" in youtube

        result = self.client.provision_demo(
            external_ref=f"agency-os:prospect:{prospect.id}",
            name=prospect.name,
            state=prospect.state or "CA",
            org_type=self._to_org_type(prospect),
            website_url=prospect.website_url or "",
            ein=prospect.ein or "",
            contact_email=contact_email,
            refresh=refresh,
            irs_subsection=metadata.get("irs_subsection") or "",
            video_url="" if is_channel else youtube,
            youtube_channel=youtube if is_channel else "",
        )
        if result.get("error"):
            return result
        return {**result, "portal_demo_url": result.get("demo_url"), "demo_url": result.get("video_demo_url")}

    def _per_view_cents(self) -> int:
        """What a sponsor pays per completed view, from u9itus, cached for 10 minutes."""
        now = time.monotonic()
        if self._rate_cache and now - self._rate_cache[0] < self.PLANS_TTL_SECONDS:
            return self._rate_cache[1]
        cents = self.FALLBACK_PER_VIEW_CENTS
        if self.client.is_configured():
            result = self.client.get_plans()
            fetched = (result.get("video") or {}).get("revenue_per_view_cents")
            if isinstance(fetched, int) and fetched > 0 and not result.get("error"):
                cents = fetched
                self._rate_cache = (now, cents)
        return cents

    def pricing_tiers(self) -> list[dict]:
        """View packs priced at u9itus's per-view rate."""
        rate = self._per_view_cents()
        return [
            {
                "key": key,
                "name": f"{label} ({views:,} views)",
                "price": views * rate / 100 if (views * rate) % 100 else views * rate // 100,
                "period": "per campaign",
                "features": self.PACK_FEATURES,
            }
            for key, label, views in self.VIEW_PACKS
        ]
