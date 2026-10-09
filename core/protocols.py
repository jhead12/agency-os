"""
Plugin protocols (contracts).

Every plugin implements one of these. They are typing.Protocol classes —
duck-typed, no inheritance required. Any object with the right attributes
and methods satisfies the protocol.
"""

from __future__ import annotations

from dataclasses import dataclass, field
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

    Optional: event_effect(event) -> EventEffect | None says what one of the
    product's events does to the pipeline. Without it, or when it returns
    shared_event_effect(event), the shared portal.* vocabulary applies. A
    product adds its own event types there (u9itus: subscription.*) without
    any change to core.
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


@dataclass(frozen=True)
class EventEffect:
    """What one product event does (Pipeline.pull_product_events). Every
    event is recorded in product_events whatever its effect."""

    # Move each of the prospect's outreach rows forward to this stage; never backward.
    stage: Optional[str] = None
    # Add an activity-log entry of this type to each outreach row (None = no entry).
    log_as: Optional[str] = None
    # Flag carried on the activity entry, e.g. "ready_to_close".
    flag: Optional[str] = None
    # Extra fields on the activity entry, e.g. {"plan": "pro"}.
    detail: dict = field(default_factory=dict)
    # Merged into prospect.metadata[portal_namespace], e.g. {"status": "claimed"}.
    portal: dict = field(default_factory=dict)
    # The demo link no longer works; clearing it lets provisioning issue a new one.
    clear_demo_link: bool = False


def shared_event_effect(event: dict) -> Optional[EventEffect]:
    """The portal.* events every demo-portal product shares. None for any other type."""
    return {
        "portal.viewed": EventEffect(stage="engaged", log_as="portal.viewed"),
        # The sandboxed video campaign demo (u9itus section 13) counts the same as a view.
        "portal.video_demo_viewed": EventEffect(stage="engaged", log_as="portal.video_demo_viewed"),
        "portal.claimed": EventEffect(stage="demo_scheduled", log_as="portal.claimed",
                                      portal={"status": "claimed"}),
        # Publishing is the buying signal: flag it, but leave the stage to the rep.
        "portal.published": EventEffect(log_as="portal_published", flag="ready_to_close",
                                        portal={"status": "published"}),
        "portal.expired": EventEffect(portal={"status": "expired"}, clear_demo_link=True),
    }.get(event.get("type"))


def event_effect(product, event: dict) -> Optional[EventEffect]:
    """The product's effect for an event, or the shared one when it doesn't define any."""
    custom = getattr(product, "event_effect", None)
    return custom(event) if callable(custom) else shared_event_effect(event)


def event_feed(product, product_key: str) -> str:
    """The key the product's event cursor and processed events are kept under.

    Products that read the same feed (u9itus's voter guide and video campaign
    products) set the same `event_feed`, so each event is applied once.
    Defaults to the product's own key.
    """
    return getattr(product, "event_feed", None) or product_key


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


@runtime_checkable
class VoiceProvider(Protocol):
    """A calling service that places prospect calls from the dashboard (Twilio, etc.).

    The rules about who may dial and whom (core/voice.py) are the same for every
    provider; a provider only speaks its service's language: browser tokens, call
    instructions and webhooks. See docs/BROWSER_CALLING.md.
    """

    key: str
    media_type: str  # of the call instructions, e.g. "application/xml" for TwiML

    def is_configured(self) -> bool:
        """Whether the credentials browser calling needs are set."""
        ...

    def access_token(self, identity: str, ttl_seconds: int = 3600) -> str:
        """A short-lived token the browser SDK connects with, for this identity."""
        ...

    def verify_webhook(self, url: str, params: dict, headers: dict) -> bool:
        """Whether a webhook request really came from the provider."""
        ...

    def parse_dial(self, params: dict) -> dict:
        """{call_sid, identity, outreach_id} from the provider's request to place a call."""
        ...

    def dial_response(self, to_number: str, caller_id: str, status_url: str) -> str:
        """Call instructions that dial `to_number` from `caller_id`, reporting the end to `status_url`."""
        ...

    def refuse_response(self, message: str) -> str:
        """Call instructions that tell the rep `message` and hang up."""
        ...

    def parse_status(self, params: dict) -> dict:
        """{call_sid, status, duration_seconds} from the provider's end-of-call webhook."""
        ...

    def end_response(self) -> str:
        """Call instructions that end the call."""
        ...
