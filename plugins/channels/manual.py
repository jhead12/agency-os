"""
Manual channel — logs a touch without sending anything.

Use this to record phone calls, in-person meetings, LinkedIn messages,
or any outreach you did manually. The pipeline stays accurate even when
touches happen outside the automated system.
"""

from __future__ import annotations

from datetime import datetime

from core.models import SendResult


class ManualChannel:
    """Logs a manual touch — no email sent, just records the activity."""

    key = "manual"

    def is_configured(self) -> bool:
        return True

    def send(self, recipient: dict, subject: str, body: str, metadata: dict) -> SendResult:
        print(f"    → [manual] Logged touch for {recipient.get('email', 'unknown')}")
        return SendResult(
            status="sent",
            provider_message_id=f"manual_{metadata.get('outreach_id', 'unknown')}",
            sent_at=datetime.now(),
        )