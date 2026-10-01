"""
Core data models for agency-os.

These are plain dataclasses — no ORM, no framework. The DB layer in core/db.py
handles persistence; these are the in-memory representations passed between
plugins and the pipeline engine.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


@dataclass
class Prospect:
    """An organization we might sell to."""

    name: str
    source: str = ""
    source_url: str = ""
    ein: Optional[str] = None
    ntee_code: Optional[str] = None
    website_url: Optional[str] = None
    address: Optional[str] = None
    city: Optional[str] = None
    state: Optional[str] = None
    zip: Optional[str] = None
    county: Optional[str] = None
    focus_area: Optional[str] = None
    annual_revenue: Optional[int] = None
    voter_engagement: bool = False
    metadata: dict = field(default_factory=dict)
    id: Optional[int] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    @property
    def slug(self) -> str:
        import re

        s = self.name.lower().strip()
        s = re.sub(r"[^a-z0-9]+", "-", s)
        return s.strip("-")


@dataclass
class Outreach:
    """A pipeline row — one prospect's journey through one campaign."""

    prospect_id: int
    campaign_id: int
    stage: str = "cold"
    touch_count: int = 0
    contact_name: Optional[str] = None
    contact_email: Optional[str] = None
    contact_phone: Optional[str] = None
    contact_title: Optional[str] = None
    script_variant: Optional[str] = None
    last_contacted_at: Optional[datetime] = None
    next_follow_up_at: Optional[datetime] = None
    demo_link: Optional[str] = None
    notes: Optional[str] = None
    activity_log: list = field(default_factory=list)
    assigned_to: Optional[str] = None
    closed_at: Optional[datetime] = None
    close_reason: Optional[str] = None
    id: Optional[int] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


@dataclass
class SendResult:
    """Result of a channel send attempt."""

    status: str  # sent | bounced | failed | skipped
    provider_message_id: Optional[str] = None
    error: Optional[str] = None
    sent_at: Optional[datetime] = None


@dataclass
class EnrichmentResult:
    """Result of a contact enrichment lookup."""

    contact_name: Optional[str] = None
    contact_email: Optional[str] = None
    contact_phone: Optional[str] = None
    contact_title: Optional[str] = None
    confidence: float = 0.0
    source: str = ""
    raw: dict = field(default_factory=dict)


@dataclass
class EmailLog:
    """A record of one email sent (or attempted)."""

    outreach_id: int
    campaign_id: int
    template_key: str
    subject: str
    body: str
    status: str = "sent"
    provider_message_id: Optional[str] = None
    sent_at: Optional[datetime] = None
    id: Optional[int] = None