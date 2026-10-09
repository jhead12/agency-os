"""
Test emails and test calls: an Owner reviews a campaign on their own devices.

- A test email renders one of the campaign's email scripts (with sample data or
  one of the campaign's prospects) and sends it through SMTP to an address the
  Owner types, with "[TEST]" in front of the subject and the usual CAN-SPAM footer.
- A test call rings a number the Owner types, from the campaign's caller ID,
  through the dashboard softphone (core/voice.py): the Owner talks from the
  browser and hears and sees the call on their phone. A person always speaks;
  nothing is played or synthesized (human callers only).

Neither touches outreach, stages or stats. Each is stored in campaign_tests and
the audit log, and limited per user per day. Test calls go only to US/Canada
(+1) numbers, and the browser never sends the number to the provider: the row
is created here and the dial webhook claims it once (voice.place_test_call).
"""

from __future__ import annotations

import re
from typing import Optional

from core import compliance, voice
from core.models import Outreach, Prospect
from core.pipeline import Pipeline

EMAIL_LIMIT_PER_DAY = 20
CALL_LIMIT_PER_DAY = 10
TEST_CALL_TTL_MINUTES = 10  # a test call must be dialed this soon after it's started
SUBJECT_PREFIX = "[TEST] "

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class TestRefused(ValueError):
    """Why a test email or call can't go out, in words for the Owner."""


def email_scripts(campaign) -> list[str]:
    """Stems of the campaign's email scripts (not phone or mail pieces), in file order."""
    scripts_dir = campaign.config_dir / "scripts"
    if not scripts_dir.exists():
        return []
    stems = []
    for path in sorted(scripts_dir.glob("*.yaml")):
        if path.name.startswith(("phone_", "mail_")):
            continue
        try:
            script = campaign.load_script(path.stem)
        except Exception:
            continue
        if isinstance(script, dict) and script.get("subject") and not script.get("mail_type"):
            stems.append(path.stem)
    return stems


def sample_prospect() -> Prospect:
    return Prospect(name="Test Community Organization", website_url="https://example.org",
                    address="123 Main St", city="Los Angeles", state="CA", zip="90001",
                    county="Los Angeles", focus_area="civic_engagement", source="test", id=0)


def _outreach_for(db, campaign, prospect_id: int) -> Optional[Outreach]:
    row = db.conn.execute(
        """SELECT o.id FROM outreach o JOIN campaigns c ON c.id = o.campaign_id
           WHERE o.prospect_id = ? AND c.name = ? LIMIT 1""",
        (prospect_id, campaign.db_name),
    ).fetchone()
    return db.get_outreach(row["id"]) if row else None


def render_email(db, registry, campaign, script_stem: str, prospect_id: int = 0) -> tuple[str, str]:
    """(subject, body) of one of the campaign's email scripts, as a prospect would get it.
    prospect_id 0 uses sample data; otherwise it must be one of this campaign's prospects."""
    if script_stem not in email_scripts(campaign):
        raise TestRefused("Pick one of this campaign's email scripts.")
    if prospect_id:
        outreach = _outreach_for(db, campaign, prospect_id)
        prospect = db.get_prospect(prospect_id) if outreach else None
        if prospect is None:
            raise TestRefused("That prospect isn't in this campaign.")
    else:
        prospect = sample_prospect()
        outreach = Outreach(prospect_id=0, campaign_id=0, contact_name="Test Contact",
                            contact_title="Executive Director")
    pipeline = Pipeline(db, registry)
    script = campaign.load_script(script_stem)
    variables = pipeline._build_variables(campaign, prospect, outreach)
    return (pipeline._render(script.get("subject", ""), variables),
            pipeline._render(script.get("body", ""), variables))


def _check_limit(db, user, kind: str, limit: int) -> None:
    if db.count_campaign_tests(user.id, kind) >= limit:
        raise TestRefused(f"You've sent {limit} test {kind}s in the last 24 hours. Try again later.")


def send_email(db, registry, campaign, user, to_email: str, script_stem: str, prospect_id: int = 0):
    """Send one test email through SMTP. Returns the campaign_tests row; raises TestRefused."""
    to_email = (to_email or "").strip()
    if not _EMAIL_RE.match(to_email):
        raise TestRefused("Enter a valid email address.")
    channel = registry.get_channel("email_smtp")
    if channel is None or not channel.is_configured():
        raise TestRefused("Test emails go out through SMTP, which isn't set up (SMTP_HOST, SMTP_USER, SMTP_PASS).")
    blocked = compliance.problem()
    if blocked:
        raise TestRefused(f"Email is off: {blocked}")
    if db.email_suppressed(to_email):
        raise TestRefused(f"{to_email} unsubscribed from our email, so it can't get test emails.")
    _check_limit(db, user, "email", EMAIL_LIMIT_PER_DAY)

    subject, body = render_email(db, registry, campaign, script_stem, prospect_id)
    result = channel.send(
        recipient={"email": to_email, "phone": "", "name": user.name or ""},
        subject=SUBJECT_PREFIX + subject,
        body=compliance.with_footer(body, to_email),
        metadata={"unsubscribe_url": compliance.unsubscribe_url(to_email), "campaign": campaign.db_name,
                  "outreach_id": 0, "template_key": script_stem, "test": True},
    )
    row = db.create_campaign_test(kind="email", campaign=campaign.db_name, user_id=user.id,
                                  destination=compliance.normalize(to_email), script=script_stem,
                                  status="sent" if result.status == "sent" else "failed",
                                  error=result.error or "")
    db.audit(user, "campaign.test_email", "campaign", campaign.db_name,
             {"to": row["destination"], "script": script_stem, "status": row["status"]})
    return row


def start_call(db, campaign, user, phone: str):
    """Record a test call the browser is about to place. Returns the campaign_tests row
    (its id is all the browser sends to the provider); raises TestRefused."""
    if voice.provider("twilio") is None:
        raise TestRefused("Calling from the dashboard isn't set up.")
    caller_id = voice.campaign_caller_id(campaign)
    if not caller_id:
        raise TestRefused("This campaign has no caller ID set (voice.caller_id in campaign.yaml).")
    number = voice.e164(phone)
    if not number:
        raise TestRefused("Enter a phone number with its area code.")
    if not number.startswith("+1") or len(number) != 12:
        raise TestRefused("Test calls go only to US and Canadian (+1) numbers.")
    if number == caller_id:
        raise TestRefused("That's the campaign's own caller ID. Enter your phone number.")
    _check_limit(db, user, "call", CALL_LIMIT_PER_DAY)
    row = db.create_campaign_test(kind="call", campaign=campaign.db_name, user_id=user.id,
                                  destination=number, caller_id=caller_id, status="pending")
    db.audit(user, "campaign.test_call", "campaign", campaign.db_name, {"to": number, "test_id": row["id"]})
    return row
