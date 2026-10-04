"""
Contact depth: how far a lead has really been worked.

A lead's value is the contact that has actually happened, so every lead
carries a contact history and a tier, the deepest step that history proves:

    mailed (1) < emailed (2) < phone_verified (3) < connected (4) < pitched (5)

Package leads bring their history from the seller (parse_history). For our
own prospects it's built from call_log, email_log and the pipeline stage
(history_for_prospect), which is what we'd sell and what a buyer's own
outreach adds to.
"""

from __future__ import annotations

import re
from typing import Any, Optional

TIERS = ["unworked", "mailed", "emailed", "phone_verified", "connected", "pitched"]
RANK = {t: i for i, t in enumerate(TIERS)}

CHANNELS = {"mail", "email", "call", "sms"}

# Call dispositions. The first group are today's call-log outcomes; the
# second are the detailed codes the guarantee needs.
CALL_OUTCOMES = {
    "completed": "Completed — spoke to someone",
    "no_answer": "No answer",
    "voicemail": "Left voicemail",
    "gatekeeper": "Gatekeeper — left message",
    "wrong_number": "Wrong number",
    "scheduled": "Call scheduled for later",
    "disconnected": "Disconnected / not in service",
    "answering_machine": "Answering machine (no message left)",
    "hung_up": "Hung up",
    "busy": "Busy signal",
    "fax_tone": "Fax / modem tone",
}
SPOKE_TO_PERSON = {"completed", "gatekeeper", "scheduled"}
REACHED_MACHINE = {"voicemail", "answering_machine"}
MAIL_OK = {"sent", "delivered", "in_transit"}
EMAIL_OK = {"sent", "delivered", "opened", "clicked", "replied"}
PITCHED_STAGES = {"engaged", "demo_scheduled", "proposal_sent", "closed_won"}

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}([T ][0-9:.]+)?$")
MAX_HISTORY = 50


def touch_rank(touch: dict) -> int:
    """The deepest tier one contact proves."""
    channel, outcome = touch.get("channel"), touch.get("outcome")
    if channel == "mail" and outcome in MAIL_OK:
        return RANK["mailed"]
    if channel == "email" and outcome in EMAIL_OK:
        return RANK["emailed"]
    if channel == "call":
        if outcome in SPOKE_TO_PERSON:
            pitched = touch.get("pitched") or (touch.get("decision_maker") and outcome != "gatekeeper")
            return RANK["pitched"] if pitched else RANK["connected"]
        if outcome in REACHED_MACHINE and touch.get("org_confirmed"):
            return RANK["phone_verified"]
    return 0


def tier_of(history: list[dict], stage: str = "") -> str:
    rank = max((touch_rank(t) for t in history), default=0)
    if stage in PITCHED_STAGES:
        rank = max(rank, RANK["pitched"])
    return TIERS[rank]


def parse_history(items: Any) -> list[dict]:
    """Whitelist a seller's contact history: known channels and outcomes only."""
    if not isinstance(items, list):
        return []
    history = []
    for item in items[:MAX_HISTORY]:
        if not isinstance(item, dict):
            continue
        channel = str(item.get("channel", "")).lower()
        outcome = str(item.get("outcome", "")).lower()
        if channel not in CHANNELS:
            continue
        if channel == "call" and outcome not in CALL_OUTCOMES:
            continue
        if channel != "call" and outcome not in MAIL_OK | EMAIL_OK | {"returned", "bounced"}:
            continue
        at = str(item.get("at", ""))[:26]
        history.append({
            "channel": channel, "outcome": outcome,
            "at": at if _DATE_RE.match(at) else "",
            "decision_maker": item.get("decision_maker") is True,
            "org_confirmed": item.get("org_confirmed") is True,
            "pitched": item.get("pitched") is True,
        })
    return history


def _email_log_channel(provider_message_id: Optional[str]) -> Optional[str]:
    """Which channel an email_log row was sent through, from the provider's id."""
    pid = provider_message_id or ""
    if pid.startswith(("psc_", "ltr_")):
        return "mail"      # Lob postcard / letter
    if pid.startswith("SM"):
        return "sms"       # Twilio message SID
    if pid.startswith("manual_"):
        return None        # a manual task, not a contact
    return "email"


def history_for_prospect(db, prospect_id: int) -> tuple[list[dict], str]:
    """Our own contact history for a prospect, and its tier."""
    c = db.conn
    history = []
    for call in c.execute(
        """SELECT outcome, decision_maker_name, called_at FROM call_log
           WHERE prospect_id = ? ORDER BY called_at""", (prospect_id,),
    ).fetchall():
        dm = (call["decision_maker_name"] or "").strip()
        history.append({
            "channel": "call", "outcome": call["outcome"], "at": call["called_at"] or "",
            "decision_maker": bool(dm) and not dm.startswith("("),
            "org_confirmed": False, "pitched": False,
        })
    for sent in c.execute(
        """SELECT e.status, e.provider_message_id, e.sent_at FROM email_log e
           JOIN outreach o ON o.id = e.outreach_id
           WHERE o.prospect_id = ? ORDER BY e.sent_at""", (prospect_id,),
    ).fetchall():
        channel = _email_log_channel(sent["provider_message_id"])
        if channel:
            history.append({"channel": channel, "outcome": sent["status"] or "", "at": sent["sent_at"] or "",
                            "decision_maker": False, "org_confirmed": False, "pitched": False})
    stages = [r["stage"] for r in c.execute(
        "SELECT stage FROM outreach WHERE prospect_id = ?", (prospect_id,)).fetchall()]
    deepest = max(stages, key=lambda s: s in PITCHED_STAGES, default="")
    return history, tier_of(history, deepest)
