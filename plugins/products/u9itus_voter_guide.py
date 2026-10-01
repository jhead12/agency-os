"""
u9itus voter guide — product plugin.

Knows how to describe the value of the u9itus voter guide to a prospect,
generate demo links to the live comparison/voter-guide page, and provide
pricing tiers for proposals.
"""

from __future__ import annotations

from typing import Optional
from urllib.parse import urlencode

from core.models import Prospect


class U9itusVoterGuideProduct:
    """The u9itus digital voter guide platform."""

    key = "u9itus_voter_guide"
    BASE_URL = "https://u9itus-production.up.railway.app"

    def describe_value(self, prospect: Prospect) -> str:
        """One-line value prop personalized to this prospect."""
        focus = (prospect.focus_area or "civic engagement").replace("_", " ")
        return (
            f"a digital voter guide platform that lets {prospect.name} distribute "
            f"personalized, nonpartisan candidate comparisons and ballot measure "
            f"explanations to your constituents — aligned with your work in {focus}"
        )

    def generate_demo_link(self, prospect: Prospect, **kwargs) -> Optional[str]:
        """Generate a link to the u9itus comparison page for a demo."""
        state = (prospect.state or "CA").lower()
        params = {"state": state}
        if kwargs.get("district"):
            params["district"] = kwargs["district"]
        if kwargs.get("candidate"):
            params["candidate"] = kwargs["candidate"]
        return f"{self.BASE_URL}/compare?" + urlencode(params)

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