"""
Plugin protocols (contracts).

Every plugin implements one of these. They are typing.Protocol classes —
duck-typed, no inheritance required. Any object with the right attributes
and methods satisfies the protocol.
"""

from __future__ import annotations

from datetime import datetime
from typing import Iterator, Protocol, Optional, runtime_checkable

from core.models import Booking, Outreach, Prospect, SendResult, EnrichmentResult


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
class DemoPortalProduct(Protocol):
    """A product that hosts a personal demo page per prospect (optional).

    Events from pull_events() use the shared vocabulary portal.viewed,
    portal.claimed, portal.published and portal.expired, and identify the
    prospect with external_ref "agency-os:prospect:<id>"; a product whose own
    API names things differently translates them inside pull_events().

    Optional extras the Plugins page uses when present:
    check_connection() -> dict and create_test_portal() -> dict.
    """

    key: str
    # Key for this product's details in prospect.metadata, and the prefix on
    # its activity-log refs ("<namespace>:<event id>").
    portal_namespace: str
    # What the dashboard calls the page, e.g. "u9itus demo page".
    portal_label: str

    def is_configured(self) -> bool:
        """Whether the product's API credentials are set."""
        ...

    def setup_hint(self) -> str:
        """What to set when is_configured() is False."""
        ...

    def provision_demo(self, prospect: Prospect, contact_email: str = "", refresh: bool = False) -> dict:
        """Create (or with refresh, renew) the prospect's demo page.

        Returns demo_url, claim_url, slug, status, expires_at; or {error: True, detail}.
        """
        ...

    def get_portal_status(self, prospect: Prospect) -> dict:
        """Current status, expiry and traffic ({views_30d, last_viewed_on}) of the page."""
        ...

    def pull_events(self, after: int = 0, limit: int = 100) -> dict:
        """Events after the cursor: {events: [...], next_cursor: int}."""
        ...


def portal_product(product) -> Optional[DemoPortalProduct]:
    """The product if it hosts demo pages, else None."""
    return product if isinstance(product, DemoPortalProduct) else None


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


@runtime_checkable
class Scheduler(Protocol):
    """A meeting-booking service (Calendly, etc.)."""

    key: str

    def is_configured(self) -> bool:
        """Whether this scheduler has the credentials it needs."""
        ...

    def booking_link(self, prospect: Prospect, outreach: Outreach) -> Optional[str]:
        """A personalized link the prospect can use to book a meeting."""
        ...

    def fetch_bookings(self, since: datetime) -> Iterator[Booking]:
        """Yield bookings with a start time at or after `since`. Never raise."""
        ...
