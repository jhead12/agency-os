"""
Twilio SMS channel.

Sends outreach as text messages via the Twilio Messages API. Only the
script body is sent — subjects are ignored. Recipients without a phone
number are skipped, so the pipeline falls through to the next channel
(e.g. email).

Requires TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, and either
TWILIO_FROM_NUMBER (E.164, e.g. +13105550123) or
TWILIO_MESSAGING_SERVICE_SID in environment.

US numbers must be registered for A2P 10DLC before Twilio will deliver
business texts. Twilio handles STOP/HELP opt-out keywords automatically.
"""

from __future__ import annotations

import os
import re
from datetime import datetime
from typing import Optional

import httpx

from core.models import SendResult

# Twilio rejects message bodies longer than this
MAX_BODY_CHARS = 1600


def normalize_phone(raw: str, default_country_code: str = "1") -> Optional[str]:
    """Best-effort E.164 normalization. Returns None if it can't be a phone number."""
    if not raw:
        return None
    raw = raw.strip()
    digits = re.sub(r"\D", "", raw)
    if raw.startswith("+"):
        return f"+{digits}" if 8 <= len(digits) <= 15 else None
    if len(digits) == 10:
        return f"+{default_country_code}{digits}"
    if len(digits) == 11 and digits.startswith(default_country_code):
        return f"+{digits}"
    return None


class SmsTwilioChannel:
    """Twilio SMS channel."""

    key = "sms_twilio"
    API_BASE = "https://api.twilio.com/2010-04-01"

    def __init__(self):
        self._sid = os.environ.get("TWILIO_ACCOUNT_SID", "")
        self._token = os.environ.get("TWILIO_AUTH_TOKEN", "")
        self._from = os.environ.get("TWILIO_FROM_NUMBER", "")
        self._service = os.environ.get("TWILIO_MESSAGING_SERVICE_SID", "")

    def is_configured(self) -> bool:
        return bool(self._sid and self._token and (self._from or self._service))

    def send(self, recipient: dict, subject: str, body: str, metadata: dict) -> SendResult:
        """Send one SMS via Twilio. Never raises."""
        if not self.is_configured():
            return SendResult(status="skipped", error="Twilio not configured")

        to = normalize_phone(recipient.get("phone") or "")
        if not to:
            return SendResult(status="skipped", error="No valid phone number")

        body = body.strip()
        if len(body) > MAX_BODY_CHARS:
            return SendResult(
                status="failed",
                error=f"SMS body is {len(body)} chars (max {MAX_BODY_CHARS})",
                sent_at=datetime.now(),
            )

        data = {"To": to, "Body": body}
        if self._service:
            data["MessagingServiceSid"] = self._service
        else:
            data["From"] = self._from

        try:
            resp = httpx.post(
                f"{self.API_BASE}/Accounts/{self._sid}/Messages.json",
                data=data,
                auth=(self._sid, self._token),
                timeout=30,
            )
            payload = resp.json()
            if resp.status_code >= 400:
                return SendResult(
                    status="failed",
                    error=f"Twilio {payload.get('code', resp.status_code)}: {payload.get('message', resp.text)}",
                    sent_at=datetime.now(),
                )

            print(f"    → [twilio] To: {to} | {body[:50]}")
            return SendResult(
                status="sent",
                provider_message_id=payload.get("sid"),
                sent_at=datetime.now(),
            )
        except Exception as exc:
            return SendResult(status="failed", error=str(exc), sent_at=datetime.now())
