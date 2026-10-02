"""
Calendly scheduler.

Two jobs:
  1. booking_link() — a personalized Calendly link for email/SMS templates
     ({{booking_link}}). Prefills the invitee's name/email and tags the link
     with utm_content=outreach-<id> so bookings match back to the right
     pipeline row even if they book with a different email.
  2. fetch_bookings() — pulls scheduled events from the Calendly API so
     `agency_os.py bookings` can move prospects to demo_scheduled.

Requires CALENDLY_SCHEDULING_URL (an event type link, e.g.
https://calendly.com/you/demo) for booking links, and CALENDLY_API_TOKEN
(Calendly → Integrations → API & Webhooks → Personal Access Token) for
syncing bookings. With only the token, links fall back to your profile page.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Iterator, Optional
from urllib.parse import urlencode

import httpx

from core.models import Booking, Outreach, Prospect

UTM_SOURCE = "agency-os"


def _parse_time(val: Optional[str]) -> Optional[datetime]:
    """Calendly UTC timestamp → local naive datetime (matches the rest of the DB)."""
    if not val:
        return None
    return datetime.fromisoformat(val.replace("Z", "+00:00")).astimezone().replace(tzinfo=None)


class CalendlyScheduler:
    """Calendly booking links + booking sync."""

    key = "calendly"
    API_BASE = "https://api.calendly.com"

    def __init__(self):
        self._token = os.environ.get("CALENDLY_API_TOKEN", "")
        self._scheduling_url = os.environ.get("CALENDLY_SCHEDULING_URL", "")
        self._user: Optional[dict] = None

    def is_configured(self) -> bool:
        return bool(self._token or self._scheduling_url)

    def booking_link(self, prospect: Prospect, outreach: Outreach) -> Optional[str]:
        base = self._scheduling_url
        if not base and self._token:
            base = (self._me() or {}).get("scheduling_url", "")
        if not base:
            return None

        params = {"utm_source": UTM_SOURCE}
        if outreach.id:
            params["utm_content"] = f"outreach-{outreach.id}"
        if outreach.contact_name:
            params["name"] = outreach.contact_name
        if outreach.contact_email:
            params["email"] = outreach.contact_email
        sep = "&" if "?" in base else "?"
        return f"{base}{sep}{urlencode(params)}"

    def fetch_bookings(self, since: datetime) -> Iterator[Booking]:
        """Yield one Booking per invitee on events starting at/after `since`. Never raises."""
        if not self._token:
            print("  ! CALENDLY_API_TOKEN not set — can't sync bookings")
            return
        me = self._me()
        if not me:
            return

        min_start = since.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        url: Optional[str] = f"{self.API_BASE}/scheduled_events"
        params: Optional[dict] = {
            "user": me["uri"],
            "min_start_time": min_start,
            "count": 100,
            "sort": "start_time:asc",
        }
        try:
            while url:
                page = self._get(url, params)
                for event in page.get("collection", []):
                    yield from self._event_bookings(event)
                url = page.get("pagination", {}).get("next_page")
                params = None  # next_page already carries the query
        except Exception as exc:
            print(f"  ! Calendly sync failed: {exc}")

    # ── Helpers ────────────────────────────────────────────────────────

    def _event_bookings(self, event: dict) -> Iterator[Booking]:
        invitees = self._get(f"{event['uri']}/invitees", {"count": 100}).get("collection", [])
        for inv in invitees:
            tracking = inv.get("tracking") or {}
            outreach_id = None
            content = tracking.get("utm_content") or ""
            if tracking.get("utm_source") == UTM_SOURCE and content.startswith("outreach-"):
                try:
                    outreach_id = int(content.removeprefix("outreach-"))
                except ValueError:
                    pass
            # An invitee can cancel while the event stays active (group events)
            status = "canceled" if "canceled" in (event.get("status"), inv.get("status")) else "active"
            yield Booking(
                external_id=inv["uri"],
                invitee_email=(inv.get("email") or "").lower(),
                invitee_name=inv.get("name") or "",
                event_name=event.get("name") or "",
                start_time=_parse_time(event.get("start_time")),
                end_time=_parse_time(event.get("end_time")),
                status=status,
                join_url=(event.get("location") or {}).get("join_url"),
                outreach_id=outreach_id,
                raw={"event": event, "invitee": inv},
            )

    def _me(self) -> Optional[dict]:
        if self._user is None:
            try:
                self._user = self._get(f"{self.API_BASE}/users/me")["resource"]
            except Exception as exc:
                print(f"  ! Calendly auth failed: {exc}")
                return None
        return self._user

    def _get(self, url: str, params: Optional[dict] = None) -> dict:
        resp = httpx.get(
            url,
            params=params,
            headers={"Authorization": f"Bearer {self._token}"},
            timeout=30,
        )
        resp.raise_for_status()
        return resp.json()
