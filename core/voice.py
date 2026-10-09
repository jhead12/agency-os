"""
Prospect calls placed from the dashboard (docs/BROWSER_CALLING.md).

These rules hold whichever service places the call (plugins/voice/): only a
Super Admin dials, only an outreach's stored number, never a prospect marked
do-not-call, and always from the campaign's own caller ID. Each call stores
the prospect's state and the version of the recording disclosure the rep was
shown, so there's a record of what was said before anything is recorded (V3).

Nothing here imports a provider; plugins/voice/ is found through the registry.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Optional

from core.registry import PluginRegistry

PLUGINS_DIR = Path(__file__).resolve().parent.parent / "plugins"
TOKEN_TTL_SECONDS = 3600

# What the rep reads first on every call, in every state. DRAFT, pending counsel
# review (docs/BROWSER_CALLING.md section 3). Never edit a published text: add the
# new wording at the end, so the version stored on past calls still means what it said.
DISCLOSURES: tuple[str, ...] = (
    "Hi, this is {rep_first_name} calling from {company_name}. Just so you know, this call "
    "is being recorded for quality and training purposes. Is that all right with you?",
)

# States treated as all-party consent for recording phone calls. Includes the
# disputed ones (DE, MI, NV, OR), which are treated as all-party. Counsel to confirm.
ALL_PARTY_CONSENT_STATES = frozenset({
    "CA", "CT", "DE", "FL", "IL", "MD", "MA", "MI", "MT", "NV", "NH", "OR", "PA", "WA",
})
US_STATES = frozenset({
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "DC", "FL", "GA", "HI", "ID", "IL", "IN",
    "IA", "KS", "KY", "LA", "ME", "MD", "MA", "MI", "MN", "MS", "MO", "MT", "NE", "NV", "NH",
    "NJ", "NM", "NY", "NC", "ND", "OH", "OK", "OR", "PA", "RI", "SC", "SD", "TN", "TX", "UT",
    "VT", "VA", "WA", "WV", "WI", "WY",
})

# Spoken to the rep (the provider's <Say>) when a call is refused.
NOT_SET_UP = "Calling from the dashboard isn't set up."
SUPER_ADMIN_ONLY = "Only a Super Admin can place calls from the dashboard."
NOT_FOUND = "That contact wasn't found."
NO_CALLER_ID = "This campaign has no caller ID set."
DO_NOT_CALL = "This person asked not to be called."
NO_NUMBER = "There's no dialable number for this contact."
TEST_EXPIRED = "This test call has expired. Start a new one from the campaign page."


# ── Numbers, consent and the disclosure ───────────────────────────────


def e164(phone) -> str:
    """A stored phone number as E.164 (+13105550123), or "" if it isn't dialable.

    US numbers get +1 (10 digits, or 11 starting with 1); numbers written with a
    leading + keep their country code.
    """
    raw = str(phone or "").strip()
    digits = "".join(ch for ch in raw if ch.isdigit())
    if raw.startswith("+") and 8 <= len(digits) <= 15:
        return f"+{digits}"
    if len(digits) == 10:
        return f"+1{digits}"
    if len(digits) == 11 and digits.startswith("1"):
        return f"+{digits}"
    return ""


def campaign_caller_id(campaign) -> str:
    """The campaign's `voice.caller_id` as E.164, or "" unless it's written with a leading +."""
    raw = campaign.voice.caller_id
    return e164(raw) if raw.startswith("+") else ""


def consent_rule(state) -> tuple[Optional[str], bool]:
    """(two-letter state or None if unknown, whether to treat the call as all-party consent).
    An unknown state is treated as all-party."""
    code = str(state or "").strip().upper()
    if code not in US_STATES:
        return None, True
    return code, code in ALL_PARTY_CONSENT_STATES


def state_label(state) -> str:
    """What the call bar shows the rep, e.g. "CA: all-party consent"."""
    code, all_party = consent_rule(state)
    if code is None:
        return "State unknown: treat as all-party consent"
    return f"{code}: {'all-party' if all_party else 'one-party'} consent"


def disclosure_version(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:12]


CURRENT_DISCLOSURE = DISCLOSURES[-1]
CURRENT_DISCLOSURE_VERSION = disclosure_version(CURRENT_DISCLOSURE)


def disclosure_text(company_name: str, rep_name: str, template: str = CURRENT_DISCLOSURE) -> str:
    first = (rep_name or "").strip().split(" ")[0] or "your caller"
    return template.format(rep_first_name=first, company_name=company_name or "our team")


# ── Providers ──────────────────────────────────────────────────────────


_providers: dict[str, Any] | None = None


def providers() -> dict[str, Any]:
    """The calling services in plugins/voice/, found once per process."""
    global _providers
    if _providers is None:
        registry = PluginRegistry()
        registry.discover(str(PLUGINS_DIR), categories=("voice",))
        _providers = registry.voice
    return _providers


def provider(key: str):
    """The provider with this key if its settings are present, else None."""
    found = providers().get(key)
    return found if found is not None and found.is_configured() else None


def identity_for(user) -> str:
    return f"user-{user.id}"


def user_id_from_identity(identity: str) -> Optional[int]:
    prefix, _, number = str(identity or "").partition("-")
    return int(number) if prefix == "user" and number.isdigit() else None


def can_dial(user, campaign) -> bool:
    """Whether the Call button should dial from the dashboard for this campaign
    (otherwise the page keeps its tel: link). Final checks happen in place_call()."""
    return bool(provider("twilio") and user.is_super_admin and campaign_caller_id(campaign)
                and user.sees_campaign(campaign))


# How a call ended → the Log call form's pre-selected outcome (core/contact_depth.CALL_OUTCOMES).
# Only a guess: the rep can change it.
OUTCOME_GUESS = {"completed": "completed", "no-answer": "no_answer", "busy": "busy",
                 "failed": "disconnected", "canceled": "disconnected"}


def call_button(user, campaign, contact_phone, prospect, do_not_call: bool) -> Optional[dict]:
    """What the softphone Call button needs for one outreach, or None to keep the tel: link.
    Carries no phone number: the server looks it up when the call is placed."""
    if not e164(contact_phone) or not can_dial(user, campaign):
        return None
    return {
        "disclosure": disclosure_text(campaign.voice.company_name, user.name),
        "state_label": state_label(prospect.state),
        "blocked": DO_NOT_CALL if do_not_call else "",
    }


def log_prefill(call) -> dict:
    """Log call form values from a finished dashboard call."""
    seconds = call["duration_seconds"] or 0
    return {
        "voice_call_id": call["id"],
        "outreach_id": call["outreach_id"],
        "outcome": OUTCOME_GUESS.get(call["status"], ""),
        "duration_minutes": max(1, -(-seconds // 60)) if seconds else "",
        "seconds": seconds,
        "status": call["status"],
    }


# ── Calls ──────────────────────────────────────────────────────────────


def place_call(db, campaigns, provider_key: str, call_sid: str, identity: str,
               outreach_id) -> tuple[Optional[dict], str]:
    """Decide whether to dial, and record the call if so.

    Returns (voice_calls row, "") to dial, or (None, reason) to refuse; the reason
    is spoken to the rep. A provider retrying the same call gets the same row back.
    """
    if not call_sid:
        return None, NOT_FOUND
    existing = db.get_voice_call_by_sid(provider_key, call_sid)
    if existing:
        return existing, ""
    user_id = user_id_from_identity(identity)
    user = db.load_current_user(user_id) if user_id else None
    if user is None or not user.is_super_admin:
        return None, SUPER_ADMIN_ONLY
    try:
        target = db.voice_call_target(int(outreach_id))
    except (TypeError, ValueError):
        target = None
    campaign = next((c for c in campaigns if target and c.db_name == target["campaign_name"]), None)
    if campaign is None or not user.sees_campaign(campaign):
        return None, NOT_FOUND
    caller_id = campaign_caller_id(campaign)
    if not caller_id:
        return None, NO_CALLER_ID
    if target["do_not_call"]:
        return None, DO_NOT_CALL
    to_number = e164(target["contact_phone"])
    if not to_number:
        return None, NO_NUMBER
    state, all_party = consent_rule(target["state"])
    call = db.create_voice_call(
        provider=provider_key, call_sid=call_sid, outreach_id=target["outreach_id"],
        campaign_id=target["campaign_id"], prospect_id=target["prospect_id"], user_id=user.id,
        caller_id=caller_id, to_number=to_number, prospect_state=state,
        all_party_consent=all_party, disclosure_version=CURRENT_DISCLOSURE_VERSION,
    )
    return call, ""


def place_test_call(db, campaigns, provider_key: str, call_sid: str, identity: str,
                    test_id, max_age_minutes: int) -> tuple[Optional[dict], str]:
    """Decide whether to dial an Owner's test call (core/campaign_tests.py).

    Only the Owner who started it, only once, only soon after, and only to the
    number they entered then, from the campaign's caller ID. Returns
    (campaign_tests row, "") to dial, or (None, reason) to refuse.
    """
    if not call_sid:
        return None, NOT_FOUND
    user_id = user_id_from_identity(identity)
    user = db.load_current_user(user_id) if user_id else None
    if user is None or not user.is_owner:
        return None, NOT_FOUND
    try:
        test = db.get_campaign_test(int(test_id))
    except (TypeError, ValueError):
        test = None
    if test is None or test["kind"] != "call" or test["user_id"] != user.id:
        return None, NOT_FOUND
    campaign = next((c for c in campaigns if c.db_name == test["campaign"]), None)
    if campaign is None or not user.sees_campaign(campaign):
        return None, NOT_FOUND
    if campaign_caller_id(campaign) != test["caller_id"]:
        return None, NO_CALLER_ID
    claimed = db.claim_test_call(test["id"], provider_key, call_sid, max_age_minutes)
    return (claimed, "") if claimed else (None, TEST_EXPIRED)


def record_status(db, provider_key: str, status: dict) -> bool:
    """Store how a call (or a test call) ended. Only the first report counts, so replays change nothing."""
    args = (provider_key, status["call_sid"], status["status"], status["duration_seconds"])
    return db.finish_voice_call(*args) or db.finish_test_call(*args)


def confirm_disclosure(db, voice_call_id: int, user) -> bool:
    """The rep pressed "Disclosure read". Only the rep who placed the call can, once."""
    return db.mark_disclosure_read(voice_call_id, user.id)
