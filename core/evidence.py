"""
Evidence that arrives by itself: bounces and returned mail from webhooks,
recorded as contact events (core/verify.py) so the lead guarantee measures
itself and the prospect page prompts for a new email.

Sources (web/app.py routes, all off until their secret is set):

- Lob   POST /webhooks/lob       letters / postcards / self-mailers returned
                                 to sender. Signed: Lob-Signature is the hex
                                 HMAC-SHA256 of "<Lob-Signature-Timestamp>.<raw body>"
                                 with LOB_WEBHOOK_SECRET; older than 5 minutes is refused.
- Smartlead POST /webhooks/smartlead?key=...   EMAIL_BOUNCE events. Smartlead
                                 doesn't sign webhooks, so the URL carries
                                 AGENCY_OS_WEBHOOK_KEY.
- Any other sender (SMTP relays, Postmark, Zapier...)
          POST /webhooks/bounce?key=...  {"email": "...", "type": "hard"|"soft", "id": "..."}

Each provider event is recorded once (by its event id), soft bounces are
ignored, and an email that matches nobody is acknowledged and dropped.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
from typing import Any, Optional

from core import verify

MAX_BODY = 256 * 1024
LOB_TOLERANCE_SECONDS = 300


def webhook_key_ok(given: str) -> bool:
    expected = os.environ.get("AGENCY_OS_WEBHOOK_KEY", "")
    return bool(expected) and hmac.compare_digest(given.encode(), expected.encode())


def lob_signature_ok(body: bytes, signature: str, timestamp: str, *, now: Optional[float] = None) -> bool:
    secret = os.environ.get("LOB_WEBHOOK_SECRET", "")
    if not secret or not signature or not timestamp:
        return False
    try:
        stamp = float(timestamp)
    except ValueError:
        return False
    if stamp > 1e12:  # Lob's docs don't say seconds or milliseconds; accept both
        stamp /= 1000
    if abs((now or time.time()) - stamp) > LOB_TOLERANCE_SECONDS:
        return False
    expected = hmac.new(secret.encode(), timestamp.encode() + b"." + body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature.strip().lower())


def _seen(db, source: str, event_id: str) -> bool:
    if not event_id:
        return False
    return db.conn.execute(
        "SELECT 1 FROM contact_events WHERE detail::jsonb ->> 'source' = ? AND detail::jsonb ->> 'event_id' = ? LIMIT 1",
        (source, event_id)).fetchone() is not None


def record_bounce(db, email: str, *, source: str, event_id: str = "", detail: Optional[dict] = None) -> int:
    """Record a hard bounce for every campaign contact with this email. Returns how many."""
    email = (email or "").strip().lower()
    if not email or "@" not in email or _seen(db, source, event_id):
        return 0
    rows = db.conn.execute(
        "SELECT prospect_id, campaign_id FROM outreach WHERE LOWER(contact_email) = ?", (email,)).fetchall()
    for r in rows:
        verify.record_event(db, r["prospect_id"], "email_bounced", email, campaign_id=r["campaign_id"],
                            detail={"source": source, "event_id": event_id, **(detail or {})})
    return len(rows)


def record_returned_mail(db, piece_id: str, *, source: str, event_id: str = "", detail: Optional[dict] = None) -> int:
    """Record returned mail for the mail piece we sent (matched by its Lob id). Returns 0 or 1."""
    if not piece_id or _seen(db, source, event_id):
        return 0
    row = db.conn.execute(
        """SELECT o.prospect_id, o.campaign_id, p.address FROM email_log e
           JOIN outreach o ON o.id = e.outreach_id JOIN prospects p ON p.id = o.prospect_id
           WHERE e.provider_message_id = ? LIMIT 1""", (piece_id,)).fetchone()
    if row is None:
        return 0
    verify.record_event(db, row["prospect_id"], "mail_returned", row["address"] or "", campaign_id=row["campaign_id"],
                        detail={"source": source, "event_id": event_id, "piece_id": piece_id, **(detail or {})})
    return 1


# ── Provider payloads ──────────────────────────────────────────────────


def handle_lob(db, payload: Any) -> int:
    """A Lob tracking event; only "<piece>.returned_to_sender" counts (not a return envelope's)."""
    if not isinstance(payload, dict):
        return 0
    event_type = str((payload.get("event_type") or {}).get("id") or "")
    if not event_type.endswith(".returned_to_sender") or ".return_envelope." in event_type:
        return 0
    body = payload.get("body") if isinstance(payload.get("body"), dict) else {}
    piece_id = str(body.get("id") or payload.get("reference_id") or "")
    return record_returned_mail(db, piece_id, source="lob", event_id=str(payload.get("id") or ""),
                                detail={"event_type": event_type})


def _first(data: dict, *paths: str) -> str:
    for path in paths:
        value: Any = data
        for part in path.split("."):
            value = value.get(part) if isinstance(value, dict) else None
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _soft(data: dict) -> bool:
    kind = _first(data, "bounce_type", "type", "bounce.type", "bounce_category").lower()
    return "soft" in kind or data.get("is_soft_bounce") is True


def handle_smartlead(db, payload: Any) -> int:
    if not isinstance(payload, dict) or str(payload.get("event_type", "")).upper() != "EMAIL_BOUNCE" or _soft(payload):
        return 0
    email = _first(payload, "to_email", "lead_email", "lead.email", "email")
    event_id = _first(payload, "event_id", "id", "stats_id") or str(payload.get("id") or "")
    return record_bounce(db, email, source="smartlead", event_id=event_id,
                         detail={"campaign": _first(payload, "campaign_name")})


def handle_generic(db, payload: Any) -> int:
    if not isinstance(payload, dict) or _soft(payload):
        return 0
    return record_bounce(db, _first(payload, "email", "recipient", "to"), source="bounce",
                         event_id=_first(payload, "id", "event_id"))


def parse(body: bytes) -> Any:
    if len(body) > MAX_BODY:
        raise ValueError("too large")
    return json.loads(body or b"{}")
