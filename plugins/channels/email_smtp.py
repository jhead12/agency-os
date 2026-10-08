"""
Direct SMTP email channel.

Sends outreach emails via plain SMTP. Good for low-volume sending or
when you don't want to use a third-party sequencer.

Requires SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASS in environment.
"""

from __future__ import annotations

import os
import smtplib
from datetime import datetime
from email.message import EmailMessage

from core.models import SendResult


class EmailSmtpChannel:
    """Direct SMTP email channel."""

    key = "email_smtp"

    def __init__(self):
        self._host = os.environ.get("SMTP_HOST", "")
        self._port = int(os.environ.get("SMTP_PORT", "587"))
        self._user = os.environ.get("SMTP_USER", "")
        self._pass = os.environ.get("SMTP_PASS", "")
        self._from = os.environ.get("SMTP_FROM", self._user)

    def is_configured(self) -> bool:
        return bool(self._host and self._user and self._pass)

    def send(self, recipient: dict, subject: str, body: str, metadata: dict) -> SendResult:
        """Send one email via SMTP. Never raises."""
        if not self.is_configured():
            return SendResult(status="skipped", error="SMTP not configured")
        if not recipient.get("email"):
            return SendResult(status="skipped", error="No email address")

        try:
            msg = EmailMessage()
            msg["Subject"] = subject
            msg["From"] = self._from
            msg["To"] = recipient["email"]
            if metadata.get("unsubscribe_url"):
                # One-click unsubscribe in the mail client (RFC 8058); core/compliance.py
                msg["List-Unsubscribe"] = f"<{metadata['unsubscribe_url']}>"
                msg["List-Unsubscribe-Post"] = "List-Unsubscribe=One-Click"
            msg.set_content(body)

            with smtplib.SMTP(self._host, self._port, timeout=30) as server:
                server.starttls()
                server.login(self._user, self._pass)
                server.send_message(msg)

            print(f"    → [smtp] To: {recipient['email']} | Subject: {subject[:50]}")
            return SendResult(
                status="sent",
                sent_at=datetime.now(),
            )
        except Exception as exc:
            return SendResult(status="failed", error=str(exc), sent_at=datetime.now())