"""
Plugin protocols (contracts).

Every plugin implements one of these. They are typing.Protocol classes —
duck-typed, no inheritance required. Any object with the right attributes
and methods satisfies the protocol.
"""

from __future__ import annotations

from typing import Iterator, Protocol, Optional, runtime_checkable

from core.models import Prospect, SendResult, EnrichmentResult


@runtime_checkable
class ProspectSource(Protocol):
    """A source of prospects to sell to."""

    key: str

    def is_configured(self) -> bool:
        """Whether this source has the keys/credentials it needs."""
        ...

    def discover(self, filters: dict) -> Iterator[Prospect]:
        """Yield prospects matching filters (state, county, ntee_codes, etc.)."""
        ...


@runtime_checkable
class Product(Protocol):
    """What you're selling. Generates value props, demo links, pricing."""

    key: str

    def describe_value(self, prospect: Prospect) -> str:
        """One-line value prop personalized to this prospect."""
        ...

    def generate_demo_link(self, prospect: Prospect, **kwargs) -> Optional[str]:
        """A URL the prospect can visit to see the product in action."""
        ...

    def pricing_tiers(self) -> list[dict]:
        """Available pricing tiers for proposals."""
        ...


@runtime_checkable
class Channel(Protocol):
    """A delivery mechanism for outreach messages."""

    key: str

    def is_configured(self) -> bool:
        """Whether this channel has the credentials it needs."""
        ...

    def send(
        self,
        recipient: dict,
        subject: str,
        body: str,
        metadata: dict,
    ) -> SendResult:
        """Send one message. Never raise — return a failed SendResult on error."""
        ...


@runtime_checkable
class Enricher(Protocol):
    """Finds contact names/emails/phones for a prospect."""

    key: str

    def is_configured(self) -> bool:
        """Whether this enricher has the API keys it needs."""
        ...

    def enrich(self, prospect: Prospect) -> EnrichmentResult:
        """Return contact info or an empty result. Never raise."""
        ...