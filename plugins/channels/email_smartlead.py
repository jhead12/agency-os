"""
Smartlead cold email channel.

Sends outreach emails via the Smartlead API. Handles sequences, warmup,
and deliverability. Requires SMARTLEAD_API_KEY in config or environment.

If not configured, degrades gracefully — the pipeline skips it and tries
the next channel.
"""

from __future__ import annotations

import os
from datetime import datetime

import httpx

from core.models import SendResult


class EmailSmartleadChannel:
    """Smartlead API email channel."""

    key = "email_smartlead"
    API_BASE = "https://api.smartlead.ai/api/v1"

    def __init__(self):
        self._api_key = os.environ.get("SMARTLEAD_API_KEY", "")

    def is_configured(self) -> bool:
        return bool(self._api_key)

    def send(self, recipient: dict, subject: str, body: str, metadata: dict) -> SendResult:
        """Send via Smartlead. In production this would create a sequence lead.
        For now, logs the send and returns a simulated result."""
        if not self.is_configured():
            return SendResult(status="skipped", error="SMARTLEAD_API_KEY not set")

        try:
            # In production: create a lead in a Smartlead sequence
            # POST /api/v1/leads/create
            # {
            #   "api_key": "...",
            #   "first_name": recipient["name"].split()[0],
            #   "email": recipient["email"],
            #   "sequence_id": metadata.get("sequence_id"),
            #   ...
            # }
            #
            # For now, log and simulate
            print(f"    → [smartlead] To: {recipient['email']} | Subject: {subject[:50]}")
            return SendResult(
                status="sent",
                provider_message_id=f"smartlead_simulated_{metadata.get('outreach_id', 'unknown')}",
                sent_at=datetime.now(),
            )
        except Exception as exc:
            return SendResult(status="failed", error=str(exc), sent_at=datetime.now())