"""
Lob direct mail channel — send postcards and letters via the Lob API.

Lob prints, stamps, and mails physical postcards/letters on demand via API.
Use this to send physical voter guide flyers, meeting invites, or follow-up
postcards to prospects who don't respond to email.

Pricing (print + postage per piece):
  Postcards: $0.905 (Developer) → $0.615 (Growth)
  Letters:   $0.828 (Developer) → $0.628 (Growth)

Requires LOB_API_KEY in environment.
Base URL: https://api.lob.com/v1
Auth: HTTP Basic with API key as username, empty password.
"""

from __future__ import annotations

import os
import httpx
from datetime import datetime
from typing import Optional

from core.models import SendResult


class LobDirectMailChannel:
    """Lob print & mail channel — sends physical postcards and letters."""

    key = "lob_direct_mail"

    BASE_URL = "https://api.lob.com/v1"

    def __init__(self):
        self._api_key = os.environ.get("LOB_API_KEY", "")
        self._from_name = os.environ.get("LOB_FROM_NAME", "")
        self._from_address_line1 = os.environ.get("LOB_FROM_ADDRESS_LINE1", "")
        self._from_address_city = os.environ.get("LOB_FROM_ADDRESS_CITY", "")
        self._from_address_state = os.environ.get("LOB_FROM_ADDRESS_STATE", "")
        self._from_address_zip = os.environ.get("LOB_FROM_ADDRESS_ZIP", "")

    def is_configured(self) -> bool:
        """Need an API key and a return address to send mail."""
        return bool(
            self._api_key
            and self._from_address_line1
            and self._from_address_city
            and self._from_address_state
            and self._from_address_zip
        )

    def send(self, recipient: dict, subject: str, body: str, metadata: dict) -> SendResult:
        """Send a postcard to the recipient's mailing address.

        Never raises — returns a failed SendResult on error.

        recipient should have: name, address, city, state, zip
        body is used as the back-side message (plain text, max 500 chars for postcards)
        subject is used as the front-side headline (max 100 chars)
        metadata can include:
          - mail_type: "postcard" (default) or "letter"
          - campaign_id: for tracking
        """
        if not self.is_configured():
            return SendResult(status="skipped", error="Lob API key or return address not configured")

        # Need a mailing address
        addr = recipient.get("address") or ""
        city = recipient.get("city") or ""
        state = recipient.get("state") or ""
        zip_code = recipient.get("zip") or ""
        name = recipient.get("name") or ""

        if not (addr and city and state and zip_code):
            return SendResult(status="skipped", error="No mailing address for prospect")

        mail_type = metadata.get("mail_type", "postcard")

        try:
            if mail_type == "letter":
                result = self._send_letter(
                    name, addr, city, state, zip_code,
                    subject, body, metadata,
                )
            else:
                result = self._send_postcard(
                    name, addr, city, state, zip_code,
                    subject, body, metadata,
                )
            return result

        except Exception as exc:
            return SendResult(
                status="failed",
                error=str(exc),
                sent_at=datetime.now(),
            )

    def _send_postcard(
        self,
        to_name: str,
        to_address: str,
        to_city: str,
        to_state: str,
        to_zip: str,
        subject: str,
        body: str,
        metadata: dict,
    ) -> SendResult:
        """Send a 4x6 postcard via Lob API.

        Front: subject (headline)
        Back: body (message, max 500 chars)
        """
        # Lob uses HTML templates for the front and back
        front_html = f"<html><body><div style='padding: 40px; font-family: sans-serif;'><h1 style='font-size: 28px; color: #1a1a1a;'>{subject[:100]}</h1></div></body></html>"

        # Truncate body to 500 chars for postcard back
        back_text = body[:500]
        back_html = f"<html><body><div style='padding: 30px; font-family: sans-serif; font-size: 14px; color: #1a1a1a; white-space: pre-wrap;'>{back_text}</div></body></html>"

        payload = {
            "to": {
                "name": to_name,
                "address_line1": to_address,
                "address_city": to_city,
                "address_state": to_state,
                "address_zip": to_zip,
            },
            "from": {
                "name": self._from_name,
                "address_line1": self._from_address_line1,
                "address_city": self._from_address_city,
                "address_state": self._from_address_state,
                "address_zip": self._from_address_zip,
            },
            "front": front_html,
            "back": back_html,
            "size": "4x6",
        }

        if metadata.get("campaign_id"):
            payload["metadata"] = {"campaign_id": str(metadata["campaign_id"])}

        resp = self._request("POST", "/postcards", json=payload)

        if resp.get("error"):
            return SendResult(
                status="failed",
                error=resp["detail"],
                sent_at=datetime.now(),
            )

        lob_id = resp.get("id", "")
        print(f"    → [lob] Postcard to {to_name}, {to_city}, {to_state} | ID: {lob_id}")
        return SendResult(
            status="sent",
            provider_message_id=lob_id,
            sent_at=datetime.now(),
        )

    def _send_letter(
        self,
        to_name: str,
        to_address: str,
        to_city: str,
        to_state: str,
        to_zip: str,
        subject: str,
        body: str,
        metadata: dict,
    ) -> SendResult:
        """Send a letter via Lob API.

        Full letter on paper — subject as heading, body as content.
        """
        # Letter HTML — full page
        letter_html = f"""<html><head><style>
            body {{ font-family: 'Helvetica Neue', Helvetica, Arial, sans-serif; font-size: 12pt; color: #1a1a1a; margin: 1in; }}
            h1 {{ font-size: 18pt; margin-bottom: 0.5in; }}
            .body {{ white-space: pre-wrap; line-height: 1.5; }}
        </style></head><body>
            <h1>{subject}</h1>
            <div class="body">{body}</div>
        </body></html>"""

        payload = {
            "to": {
                "name": to_name,
                "address_line1": to_address,
                "address_city": to_city,
                "address_state": to_state,
                "address_zip": to_zip,
            },
            "from": {
                "name": self._from_name,
                "address_line1": self._from_address_line1,
                "address_city": self._from_address_city,
                "address_state": self._from_address_state,
                "address_zip": self._from_address_zip,
            },
            "file": letter_html,
            "color": True,
        }

        if metadata.get("campaign_id"):
            payload["metadata"] = {"campaign_id": str(metadata["campaign_id"])}

        resp = self._request("POST", "/letters", json=payload)

        if resp.get("error"):
            return SendResult(
                status="failed",
                error=resp["detail"],
                sent_at=datetime.now(),
            )

        lob_id = resp.get("id", "")
        print(f"    → [lob] Letter to {to_name}, {to_city}, {to_state} | ID: {lob_id}")
        return SendResult(
            status="sent",
            provider_message_id=lob_id,
            sent_at=datetime.now(),
        )

    def _request(self, method: str, path: str, json: Optional[dict] = None) -> dict:
        """Make an authenticated request to the Lob API.

        Lob uses HTTP Basic Auth with the API key as the username
        and an empty password.
        """
        url = f"{self.BASE_URL}{path}"

        try:
            with httpx.Client(timeout=30) as client:
                resp = client.request(
                    method,
                    url,
                    json=json,
                    auth=(self._api_key, ""),
                    headers={
                        "Content-Type": "application/json",
                        "Accept": "application/json",
                    },
                )

                if resp.status_code < 300:
                    return resp.json()

                return {
                    "error": True,
                    "status": resp.status_code,
                    "detail": f"Lob API error ({resp.status_code}): {resp.text[:300]}",
                }

        except httpx.TimeoutException:
            return {"error": True, "detail": "Lob API request timed out"}
        except httpx.ConnectError:
            return {"error": True, "detail": "Could not connect to Lob API"}
        except Exception as exc:
            return {"error": True, "detail": str(exc)}