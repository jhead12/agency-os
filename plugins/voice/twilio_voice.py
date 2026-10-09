"""
Twilio Voice: prospect calls placed from the dashboard (docs/BROWSER_CALLING.md).

The browser connects with the Twilio Voice JS SDK, using a token from
GET /voice/token. Twilio then asks the TwiML App's Voice URL
(POST /webhooks/voice/twilio/dial) what to do, and agency-os answers with a
<Dial> to the outreach's number. Who may dial and whom is decided in
core/voice.py; this module only speaks Twilio.

Requires TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN (shared with sms_twilio),
TWILIO_API_KEY_SID and TWILIO_API_KEY_SECRET (Account → API keys), and
TWILIO_TWIML_APP_SID (Voice → TwiML Apps, with its Voice URL set to
https://<dashboard>/webhooks/voice/twilio/dial).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
from typing import Optional
from xml.sax.saxutils import escape, quoteattr

REQUIRED_ENV = ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_API_KEY_SID",
                "TWILIO_API_KEY_SECRET", "TWILIO_TWIML_APP_SID")
# Twilio's DialCallStatus values; "answered" can appear when the call was bridged.
STATUSES = {"completed", "answered", "busy", "no-answer", "failed", "canceled"}


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _twiml(body: str) -> str:
    return f'<?xml version="1.0" encoding="UTF-8"?><Response>{body}</Response>'


class TwilioVoice:
    """Browser calling through Twilio Voice."""

    key = "twilio"
    media_type = "application/xml"

    def is_configured(self) -> bool:
        return all(os.environ.get(name) for name in REQUIRED_ENV)

    def access_token(self, identity: str, ttl_seconds: int = 3600, now: Optional[float] = None) -> str:
        """A Twilio access token (an HS256 JWT signed with the API key secret) with a
        Voice grant for outgoing calls through the TwiML App. Incoming calls aren't allowed."""
        issued = int(time.time() if now is None else now)
        key_sid = os.environ["TWILIO_API_KEY_SID"]
        header = {"typ": "JWT", "alg": "HS256", "cty": "twilio-fpa;v=1"}
        payload = {
            "jti": f"{key_sid}-{issued}",
            "iss": key_sid,
            "sub": os.environ["TWILIO_ACCOUNT_SID"],
            "exp": issued + ttl_seconds,
            "grants": {
                "identity": identity,
                "voice": {"outgoing": {"application_sid": os.environ["TWILIO_TWIML_APP_SID"]}},
            },
        }
        signing_input = ".".join(_b64url(json.dumps(part, separators=(",", ":")).encode())
                                 for part in (header, payload))
        signature = hmac.new(os.environ["TWILIO_API_KEY_SECRET"].encode(), signing_input.encode(),
                             hashlib.sha256).digest()
        return f"{signing_input}.{_b64url(signature)}"

    def verify_webhook(self, url: str, params: dict, headers: dict) -> bool:
        """Twilio's X-Twilio-Signature: base64 HMAC-SHA1, keyed with the auth token, of
        the full URL followed by each POST parameter's name and value, sorted by name."""
        token = os.environ.get("TWILIO_AUTH_TOKEN", "")
        given = headers.get("x-twilio-signature", "")
        if not token or not given:
            return False
        data = url + "".join(f"{name}{params[name]}" for name in sorted(params))
        expected = base64.b64encode(hmac.new(token.encode(), data.encode(), hashlib.sha1).digest()).decode()
        return hmac.compare_digest(given, expected)

    def parse_dial(self, params: dict) -> dict:
        caller = str(params.get("From") or "")
        return {
            "call_sid": str(params.get("CallSid") or ""),
            "identity": caller.removeprefix("client:") if caller.startswith("client:") else "",
            "outreach_id": str(params.get("outreach_id") or ""),
            "test_call_id": str(params.get("test_call_id") or ""),
        }

    def dial_response(self, to_number: str, caller_id: str, status_url: str) -> str:
        return _twiml(f'<Dial callerId={quoteattr(caller_id)} answerOnBridge="true" '
                      f'action={quoteattr(status_url)} method="POST">'
                      f"<Number>{escape(to_number)}</Number></Dial>")

    def refuse_response(self, message: str) -> str:
        return _twiml(f"<Say>{escape(message)}</Say><Hangup/>")

    def parse_status(self, params: dict) -> dict:
        status = str(params.get("DialCallStatus") or "")
        try:
            duration = int(params.get("DialCallDuration") or 0)
        except ValueError:
            duration = 0
        return {
            "call_sid": str(params.get("CallSid") or ""),
            "status": "completed" if status == "answered" else (status if status in STATUSES else "failed"),
            "duration_seconds": duration,
        }

    def end_response(self) -> str:
        return _twiml("<Hangup/>")
