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
import re
import httpx
from datetime import datetime
from typing import Optional

from core.models import SendResult

# Designs live in Lob's HTML template editor; a mail template can point at one.
LOB_TEMPLATES_URL = "https://dashboard.lob.com/templates"
TEMPLATE_ID_RE = re.compile(r"^tmpl_[A-Za-z0-9]+$")


def lob_template_url(template_id: str = "") -> str:
    """Link to a template in Lob's editor, or to the template list to create one."""
    if template_id and TEMPLATE_ID_RE.match(template_id):
        return f"{LOB_TEMPLATES_URL}/{template_id}"
    return LOB_TEMPLATES_URL


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
        """Send a postcard or letter to the recipient's mailing address.

        Never raises — returns a failed SendResult on error.

        For postcards: subject → front headline, body → back message (500 char max)
        For letters: subject → letter heading, body → letter content

        If metadata contains 'mail_template' (a dict with front/back or subject/body
        and mail_type), uses that instead of the generic subject/body. This lets
        campaigns define dedicated mail templates in scripts/mail_*.yaml.

        A mail template can instead name Lob HTML templates (front_template_id /
        back_template_id for postcards, template_id for letters). Lob then
        renders the design, filling {{variables}} from metadata['variables'].

        recipient should have: name or company, address, city, state, zip
        metadata can include:
          - mail_type: "postcard" (default) or "letter"
          - mail_template: dict from a mail_*.yaml script (already rendered)
          - variables: template variables, sent to Lob as merge_variables
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
        company = recipient.get("company") or ""

        if not (addr and city and state and zip_code):
            return SendResult(status="skipped", error="No mailing address for prospect")

        mail_type = metadata.get("mail_type", "postcard")
        mail_template = metadata.get("mail_template") or {}

        # If a mail template is provided, use its content instead of subject/body
        if mail_template:
            mail_type = mail_template.get("mail_type", mail_type)
            if mail_type == "letter":
                subject = mail_template.get("subject", subject)
                body = mail_template.get("body", body)
            else:
                subject = mail_template.get("front", subject)
                body = mail_template.get("back", body)

        to = {
            "name": name,
            "company": company,
            "address_line1": addr,
            "address_city": city,
            "address_state": state,
            "address_zip": zip_code,
        }
        to = {k: v for k, v in to.items() if v}
        merge_variables = {k: str(v) for k, v in (metadata.get("variables") or {}).items()}

        try:
            if mail_type == "letter":
                result = self._send_letter(
                    to, subject, body, metadata,
                    template_id=mail_template.get("template_id", ""),
                    merge_variables=merge_variables,
                )
            else:
                result = self._send_postcard(
                    to, subject, body, metadata,
                    front_template_id=mail_template.get("front_template_id", ""),
                    back_template_id=mail_template.get("back_template_id", ""),
                    merge_variables=merge_variables,
                )
            return result

        except Exception as exc:
            return SendResult(
                status="failed",
                error=str(exc),
                sent_at=datetime.now(),
            )

    def _from_address(self) -> dict:
        return {
            "name": self._from_name,
            "address_line1": self._from_address_line1,
            "address_city": self._from_address_city,
            "address_state": self._from_address_state,
            "address_zip": self._from_address_zip,
        }

    def _send_postcard(
        self,
        to: dict,
        subject: str,
        body: str,
        metadata: dict,
        front_template_id: str = "",
        back_template_id: str = "",
        merge_variables: Optional[dict] = None,
    ) -> SendResult:
        """Send a 4x6 postcard via Lob API.

        Front: Lob template front_template_id, else subject (headline)
        Back: Lob template back_template_id, else body (message, max 500 chars)
        """
        # Lob takes a saved template ID or an HTML string for each side
        front = front_template_id or f"<html><body><div style='padding: 40px; font-family: sans-serif;'><h1 style='font-size: 28px; color: #1a1a1a;'>{subject[:100]}</h1></div></body></html>"

        # Truncate body to 500 chars for postcard back
        back_text = body[:500]
        back = back_template_id or f"<html><body><div style='padding: 30px; font-family: sans-serif; font-size: 14px; color: #1a1a1a; white-space: pre-wrap;'>{back_text}</div></body></html>"

        payload = {
            "to": to,
            "from": self._from_address(),
            "front": front,
            "back": back,
            "size": "4x6",
        }
        if (front_template_id or back_template_id) and merge_variables:
            payload["merge_variables"] = merge_variables

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
        print(f"    → [lob] Postcard to {to.get('name') or to.get('company')}, "
              f"{to['address_city']}, {to['address_state']} | ID: {lob_id}")
        return SendResult(
            status="sent",
            provider_message_id=lob_id,
            sent_at=datetime.now(),
        )

    def _send_letter(
        self,
        to: dict,
        subject: str,
        body: str,
        metadata: dict,
        template_id: str = "",
        merge_variables: Optional[dict] = None,
    ) -> SendResult:
        """Send a letter via Lob API.

        Full letter on paper — Lob template template_id, else subject as
        heading and body as content.
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
            "to": to,
            "from": self._from_address(),
            "file": template_id or letter_html,
            "color": True,
        }
        if template_id and merge_variables:
            payload["merge_variables"] = merge_variables

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
        print(f"    → [lob] Letter to {to.get('name') or to.get('company')}, "
              f"{to['address_city']}, {to['address_state']} | ID: {lob_id}")
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