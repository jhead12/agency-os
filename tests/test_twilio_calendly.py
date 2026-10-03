"""
Tests for the Twilio SMS channel, Calendly scheduler, and their pipeline wiring.

Run: python -m pytest tests/
"""

import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.campaign import CadenceStep, CampaignConfig  # noqa: E402
from core.db import Database  # noqa: E402
from core.models import Booking, Outreach, Prospect, SendResult  # noqa: E402
from core.pipeline import Pipeline  # noqa: E402
from core.registry import PluginRegistry  # noqa: E402
from plugins.channels import sms_twilio  # noqa: E402
from plugins.channels.sms_twilio import SmsTwilioChannel, normalize_phone  # noqa: E402
from plugins.schedulers import calendly  # noqa: E402
from plugins.schedulers.calendly import CalendlyScheduler  # noqa: E402


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.text = str(payload)

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


# ── Twilio ─────────────────────────────────────────────────────────────


@pytest.fixture
def twilio_env(monkeypatch):
    monkeypatch.setenv("TWILIO_ACCOUNT_SID", "AC123")
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "tok")
    monkeypatch.setenv("TWILIO_FROM_NUMBER", "+13105550000")
    monkeypatch.delenv("TWILIO_MESSAGING_SERVICE_SID", raising=False)


@pytest.mark.parametrize("raw,expected", [
    ("(310) 555-0123", "+13105550123"),
    ("1-310-555-0123", "+13105550123"),
    ("+44 20 7946 0958", "+442079460958"),
    ("555-0123", None),
    ("", None),
])
def test_normalize_phone(raw, expected):
    assert normalize_phone(raw) == expected


def test_twilio_sends_sms(twilio_env, monkeypatch):
    calls = []

    def fake_post(url, data, auth, timeout):
        calls.append((url, data, auth))
        return FakeResponse({"sid": "SM999"}, 201)

    monkeypatch.setattr(sms_twilio.httpx, "post", fake_post)
    result = SmsTwilioChannel().send({"phone": "310-555-0123"}, "ignored", " Hi there ", {})

    assert result.status == "sent"
    assert result.provider_message_id == "SM999"
    url, data, auth = calls[0]
    assert url.endswith("/Accounts/AC123/Messages.json")
    assert data == {"To": "+13105550123", "Body": "Hi there", "From": "+13105550000"}
    assert auth == ("AC123", "tok")


def test_twilio_skips_without_phone(twilio_env):
    assert SmsTwilioChannel().send({"email": "a@b.org"}, "", "Hi", {}).status == "skipped"


def test_twilio_reports_api_error(twilio_env, monkeypatch):
    monkeypatch.setattr(sms_twilio.httpx, "post",
                        lambda *a, **k: FakeResponse({"code": 21211, "message": "Invalid 'To'"}, 400))
    result = SmsTwilioChannel().send({"phone": "3105550123"}, "", "Hi", {})
    assert result.status == "failed"
    assert "21211" in result.error


def test_twilio_unconfigured(monkeypatch):
    for var in ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_FROM_NUMBER", "TWILIO_MESSAGING_SERVICE_SID"):
        monkeypatch.delenv(var, raising=False)
    assert not SmsTwilioChannel().is_configured()


# ── Calendly ───────────────────────────────────────────────────────────


def test_calendly_booking_link(monkeypatch):
    monkeypatch.setenv("CALENDLY_SCHEDULING_URL", "https://calendly.com/me/demo")
    monkeypatch.delenv("CALENDLY_API_TOKEN", raising=False)
    outreach = Outreach(prospect_id=1, campaign_id=1, id=42,
                        contact_name="Ana Ruiz", contact_email="ana@org.org")
    link = CalendlyScheduler().booking_link(Prospect(name="Org"), outreach)
    assert link.startswith("https://calendly.com/me/demo?")
    assert "utm_source=agency-os" in link
    assert "utm_content=outreach-42" in link
    assert "name=Ana+Ruiz" in link
    assert "email=ana%40org.org" in link


def test_calendly_fetch_bookings(monkeypatch):
    monkeypatch.setenv("CALENDLY_API_TOKEN", "tok")
    event_uri = "https://api.calendly.com/scheduled_events/EV1"
    responses = {
        "https://api.calendly.com/users/me": {"resource": {"uri": "https://api.calendly.com/users/U1"}},
        "https://api.calendly.com/scheduled_events": {
            "collection": [{
                "uri": event_uri, "name": "Demo", "status": "active",
                "start_time": "2026-10-10T17:00:00.000000Z",
                "end_time": "2026-10-10T17:30:00.000000Z",
                "location": {"join_url": "https://zoom.us/j/1"},
            }],
            "pagination": {"next_page": None},
        },
        f"{event_uri}/invitees": {"collection": [{
            "uri": f"{event_uri}/invitees/IN1", "email": "Ana@Org.org", "name": "Ana Ruiz",
            "status": "active",
            "tracking": {"utm_source": "agency-os", "utm_content": "outreach-42"},
        }]},
    }
    seen_params = {}

    def fake_get(url, params=None, headers=None, timeout=None):
        seen_params[url] = params
        return FakeResponse(responses[url])

    monkeypatch.setattr(calendly.httpx, "get", fake_get)
    bookings = list(CalendlyScheduler().fetch_bookings(datetime(2026, 10, 1)))

    assert len(bookings) == 1
    b = bookings[0]
    assert b.outreach_id == 42
    assert b.invitee_email == "ana@org.org"
    assert b.status == "active"
    assert b.join_url == "https://zoom.us/j/1"
    assert b.start_time is not None
    assert seen_params["https://api.calendly.com/scheduled_events"]["user"] == "https://api.calendly.com/users/U1"


# ── Pipeline wiring ────────────────────────────────────────────────────


class FakeScheduler:
    key = "fake_sched"

    def __init__(self):
        self.bookings = []

    def is_configured(self):
        return True

    def booking_link(self, prospect, outreach):
        return f"https://book.example/{outreach.id}"

    def fetch_bookings(self, since):
        yield from self.bookings


class RecordingChannel:
    def __init__(self, key, needs):
        self.key, self.needs, self.sent = key, needs, []

    def is_configured(self):
        return True

    def send(self, recipient, subject, body, metadata):
        if not recipient.get(self.needs):
            return SendResult(status="skipped")
        self.sent.append((recipient, body))
        return SendResult(status="sent", sent_at=datetime.now())


@pytest.fixture
def env(tmp_path):
    scripts = tmp_path / "campaign" / "scripts"
    scripts.mkdir(parents=True)
    (scripts / "00_hello.yaml").write_text("key: hello\nsubject: Hi\nbody: 'Book here: {{booking_link}}'\n")

    campaign = CampaignConfig(
        name="Test Campaign", product="none", prospect_sources=[],
        channels=["sms", "email"], scheduler="fake_sched",
        cadence=[CadenceStep(touch=0, delay_days=3, script="00_hello", next_stage="contacted")],
        config_dir=tmp_path / "campaign",
    )
    db = Database(str(tmp_path / "test.sqlite"))
    campaign_id = db.upsert_campaign(campaign.db_name, str(campaign.config_dir))

    registry = PluginRegistry()
    registry.schedulers["fake_sched"] = FakeScheduler()
    registry.channels["sms"] = RecordingChannel("sms", "phone")
    registry.channels["email"] = RecordingChannel("email", "email")

    def add_outreach(email=None, phone=None, stage="cold"):
        pid = db.upsert_prospect(Prospect(name=f"Org {email or phone}", ein=email or phone))
        oid = db.upsert_outreach(pid, campaign_id)
        db.update_outreach(oid, {"contact_email": email, "contact_phone": phone, "stage": stage})
        return oid

    return db, Pipeline(db, registry), registry, campaign, add_outreach


def test_enqueue_falls_through_channels_and_renders_booking_link(env):
    db, pipeline, registry, campaign, add = env
    sms_id = add(phone="3105550123")
    email_id = add(email="x@org.org")
    add(email="booked@org.org", stage="demo_scheduled")  # out of sequence — must not be emailed

    stats = pipeline.enqueue_outreach(campaign)

    assert stats["sent"] == 2
    sms_body = registry.channels["sms"].sent[0][1]
    assert sms_body == f"Book here: https://book.example/{sms_id}"
    assert [r["email"] for r, _ in registry.channels["email"].sent] == ["x@org.org"]
    assert db.get_outreach(email_id).stage == "contacted"


def test_step_channels_override(env):
    db, pipeline, registry, campaign, add = env
    add(email="x@org.org", phone="3105550123")
    campaign.cadence[0].channels = ["email"]
    pipeline.enqueue_outreach(campaign)
    assert registry.channels["sms"].sent == []
    assert len(registry.channels["email"].sent) == 1


def test_sync_bookings_books_dedupes_and_cancels(env):
    db, pipeline, registry, campaign, add = env
    oid = add(email="ana@org.org", stage="contacted")
    start = datetime.now() + timedelta(days=3)
    sched = registry.schedulers["fake_sched"]
    sched.bookings = [Booking(external_id="inv1", invitee_email="ANA@org.org", start_time=start)]

    assert pipeline.sync_bookings(campaign)["booked"] == 1
    o = db.get_outreach(oid)
    assert o.stage == "demo_scheduled"
    assert o.next_follow_up_at.replace(microsecond=0) == start.replace(microsecond=0)
    assert o.activity_log[-1]["type"] == "meeting_booked"

    assert pipeline.sync_bookings(campaign)["unchanged"] == 1

    sched.bookings = [Booking(external_id="inv1", invitee_email="ana@org.org",
                              start_time=start, status="canceled")]
    assert pipeline.sync_bookings(campaign)["canceled"] == 1
    assert db.get_outreach(oid).stage == "engaged"


def test_sync_bookings_matches_by_outreach_id_and_keeps_later_stage(env):
    db, pipeline, registry, campaign, add = env
    oid = add(email="ana@org.org", stage="proposal_sent")
    registry.schedulers["fake_sched"].bookings = [
        Booking(external_id="inv2", invitee_email="other@gmail.com", outreach_id=oid,
                start_time=datetime.now() + timedelta(days=1)),
        Booking(external_id="inv3", invitee_email="stranger@nowhere.org"),
    ]
    stats = pipeline.sync_bookings(campaign)
    assert stats["booked"] == 1 and stats["unmatched"] == 1
    assert db.get_outreach(oid).stage == "proposal_sent"


def test_canceled_meeting_keeps_a_prospect_who_claimed_their_portal(env):
    # A10: claiming the u9itus page put them in the product; a canceled call doesn't undo that.
    db, pipeline, registry, campaign, add = env
    oid = add(email="ana@org.org", stage="demo_scheduled")
    db.update_outreach(oid, {"activity_log": '[{"type": "portal.claimed", "ref": "u9itus:5"}]'})
    sched = registry.schedulers["fake_sched"]
    sched.bookings = [Booking(external_id="inv9", invitee_email="ana@org.org", status="canceled")]

    assert pipeline.sync_bookings(campaign)["canceled"] == 1
    assert db.get_outreach(oid).stage == "demo_scheduled"
