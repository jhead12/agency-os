"""
Calendar feed — ICS/iCal generator for calendar sync.

Generates a standard .ics feed that any calendar app (Google Calendar,
Apple Calendar, Outlook) can subscribe to. The feed auto-updates as
new follow-ups and call next-steps are scheduled.

Usage:
    /calendar.ics              — all upcoming events
    /calendar.ics?days=30     — next 30 days only

The feed is read-only (subscribe, not sync). Calendar apps refresh
subscribed feeds every few hours. For push sync, use the export
endpoint to download a one-time .ics file.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional


def generate_ics(events: list[dict], calendar_name: str = "agency-os") -> str:
    """Generate an ICS calendar string from a list of event dicts.

    Each event dict should have:
        - uid: unique identifier
        - title: event title
        - description: event description (optional)
        - start: datetime object
        - end: datetime object (optional, defaults to start + 30 min)
        - location: location string (optional)
    """
    now = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")

    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//agency-os//Calendar//EN",
        f"X-WR-CALNAME:{calendar_name}",
        "X-WR-TIMEZONE:America/Los_Angeles",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        f"DTSTAMP:{now}",
    ]

    for event in events:
        start = event.get("start")
        end = event.get("end") or (start + timedelta(minutes=30) if start else None)
        if not start or not end:
            continue

        uid = event.get("uid", f"agency-os-{hash(str(start))}")
        title = _escape_ics(event.get("title", "Follow-up"))
        description = _escape_ics(event.get("description", ""))
        location = _escape_ics(event.get("location", ""))

        dtstart = start.strftime("%Y%m%dT%H%M%S")
        dtend = end.strftime("%Y%m%dT%H%M%S")

        lines.extend([
            "BEGIN:VEVENT",
            f"UID:{uid}@agency-os",
            f"DTSTART:{dtstart}",
            f"DTEND:{dtend}",
            f"SUMMARY:{title}",
        ])

        if description:
            lines.append(f"DESCRIPTION:{description}")
        if location:
            lines.append(f"LOCATION:{location}")

        lines.extend([
            f"DTSTAMP:{now}",
            "BEGIN:VALARM",
            "TRIGGER:-PT15M",
            "ACTION:DISPLAY",
            f"DESCRIPTION:{title}",
            "END:VALARM",
            "END:VEVENT",
        ])

    lines.append("END:VCALENDAR")
    return "\r\n".join(lines)


def _escape_ics(text: str) -> str:
    """Escape special characters for ICS format."""
    if not text:
        return ""
    text = text.replace("\\", "\\\\")
    text = text.replace(";", "\\;")
    text = text.replace(",", "\\,")
    text = text.replace("\n", "\\n")
    text = text.replace("\r", "")
    return text