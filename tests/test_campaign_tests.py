"""
Test emails and test calls an Owner sends to their own devices (core/campaign_tests.py).

Run: TEST_DATABASE_URL=postgresql://localhost/agency_os_test python -m pytest tests/test_campaign_tests.py
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import access, campaign_tests, voice  # noqa: E402
from core.models import Prospect, SendResult  # noqa: E402
from plugins.channels.email_smtp import EmailSmtpChannel  # noqa: E402
from tests.test_access import client_for, db, make_user  # noqa: E402,F401
from tests.test_voice import CALLER_ID, CAMPAIGN_YAML, post_signed, twilio_env  # noqa: E402,F401

ORIGIN = {"origin": "http://testserver"}
EMAIL_SCRIPT = """key: cold
subject: "Hello {{org_name}}"
body: |
  Hi {{contact_first}}, we help groups in {{city}}.
"""
URL = "/admin/campaigns/voice-test"


@pytest.fixture
def setup(db, twilio_env, monkeypatch):
    """The voice test campaign with an email, a phone and a mail script, a fake SMTP
    server, a Super Admin, an Owner and a Viewer."""
    for name, value in {"SMTP_HOST": "smtp.test", "SMTP_USER": "u", "SMTP_PASS": "p"}.items():
        monkeypatch.setenv(name, value)
    sent = []

    def fake_send(self, recipient, subject, body, metadata):
        sent.append({"to": recipient["email"], "subject": subject, "body": body, "metadata": metadata})
        return SendResult(status="sent")

    monkeypatch.setattr(EmailSmtpChannel, "send", fake_send)
    db.save_campaign_file("voice-test/campaign.yaml", CAMPAIGN_YAML)
    db.save_campaign_file("voice-test/scripts/00_cold.yaml", EMAIL_SCRIPT)
    db.save_campaign_file("voice-test/scripts/phone_00_call.yaml", "key: call\nopening: Hi\n")
    db.save_campaign_file("voice-test/scripts/mail_00_card.yaml", "key: card\nmail_type: postcard\nsubject: x\n")
    campaign_id = db.upsert_campaign("voice-test", "x")
    return {
        "db": db, "sent": sent, "campaign_id": campaign_id,
        "boss": make_user(db, "boss@x.com", access.SUPER_ADMIN_ROLE),
        "owner": make_user(db, "owner@x.com", access.OWNER_ROLE),
        "viewer": make_user(db, "viewer@x.com", "Viewer"),
    }


def rows_of(db, kind):
    return db.conn.execute("SELECT * FROM campaign_tests WHERE kind = ? ORDER BY id", (kind,)).fetchall()


def start_call(client, phone="(213) 555-0199"):
    return client.post(f"{URL}/test-call", data={"phone": phone}, headers=ORIGIN)


def dial_test(user_id, test_id, call_sid="CT1"):
    return post_signed(client_for(), "/webhooks/voice/twilio/dial",
                       {"CallSid": call_sid, "From": f"client:user-{user_id}", "test_call_id": str(test_id)})


# ── Test emails ────────────────────────────────────────────────────────


def test_page_offers_only_email_scripts(setup):
    page = client_for("owner@x.com").get(URL).text
    assert 'id="test"' in page and '<option value="00_cold">' in page
    assert "phone_00_call" not in page and "mail_00_card" not in page
    assert "Call my phone" in page and 'data-test-url="/admin/campaigns/voice-test/test-call"' in page


def test_owner_sends_a_test_email_with_sample_data(setup):
    db = setup["db"]
    r = client_for("owner@x.com").post(f"{URL}/test-email", headers=ORIGIN,
                                       data={"to_email": "Me@Example.com", "script": "00_cold"})
    assert r.status_code == 303 and "test_msg=" in r.headers["location"]
    [mail] = setup["sent"]
    assert mail["to"] == "Me@Example.com" and mail["subject"] == "[TEST] Hello Test Community Organization"
    assert "Hi Test, we help groups in Los Angeles." in mail["body"] and "Unsubscribe:" in mail["body"]
    assert mail["metadata"]["test"] is True and mail["metadata"]["unsubscribe_url"]
    [row] = rows_of(db, "email")
    assert (row["user_id"], row["destination"], row["script"], row["status"]) == (setup["owner"], "me@example.com", "00_cold", "sent")
    assert db.conn.execute("SELECT COUNT(*) FROM audit_log WHERE action = 'campaign.test_email'").fetchone()[0] == 1
    assert db.conn.execute("SELECT COUNT(*) FROM outreach").fetchone()[0] == 0


def test_test_email_can_use_a_prospect_of_this_campaign_only(setup):
    db = setup["db"]
    pid = db.upsert_prospect(Prospect(name="Civic Org", city="Pasadena", state="CA"))
    oid = db.upsert_outreach(pid, setup["campaign_id"])
    db.update_outreach(oid, {"contact_name": "Dana Lee", "contact_email": "dana@civic.org"})
    other = db.upsert_prospect(Prospect(name="Elsewhere Org", city="Fresno", state="CA"))
    owner = client_for("owner@x.com")

    owner.post(f"{URL}/test-email", headers=ORIGIN, data={"to_email": "me@x.com", "script": "00_cold", "prospect_id": pid})
    [mail] = setup["sent"]
    assert mail["to"] == "me@x.com" and mail["subject"] == "[TEST] Hello Civic Org"
    assert "Hi Dana, we help groups in Pasadena." in mail["body"]
    assert db.get_outreach(oid).touch_count == 0  # the prospect's sequence doesn't move

    r = owner.post(f"{URL}/test-email", headers=ORIGIN, data={"to_email": "me@x.com", "script": "00_cold", "prospect_id": other})
    assert "isn%27t%20in%20this%20campaign" in r.headers["location"] and len(setup["sent"]) == 1


@pytest.mark.parametrize("data, error", [
    ({"to_email": "not-an-email", "script": "00_cold"}, "valid%20email"),
    ({"to_email": "me@x.com", "script": "phone_00_call"}, "email%20scripts"),
    ({"to_email": "me@x.com", "script": "../../etc/passwd"}, "email%20scripts"),
])
def test_bad_test_emails_are_refused(setup, data, error):
    r = client_for("owner@x.com").post(f"{URL}/test-email", headers=ORIGIN, data=data)
    assert "test_error=" in r.headers["location"] and error in r.headers["location"]
    assert setup["sent"] == [] and rows_of(setup["db"], "email") == []


def test_test_email_respects_unsubscribes_smtp_and_can_spam(setup, monkeypatch):
    db = setup["db"]
    owner = client_for("owner@x.com")
    db.conn.execute("INSERT INTO email_suppressions (email, reason, source) VALUES ('gone@x.com', 'unsubscribed', 'link')")
    r = owner.post(f"{URL}/test-email", headers=ORIGIN, data={"to_email": "Gone@x.com", "script": "00_cold"})
    assert "unsubscribed" in r.headers["location"]

    monkeypatch.delenv("AGENCY_OS_POSTAL_ADDRESS")
    r = owner.post(f"{URL}/test-email", headers=ORIGIN, data={"to_email": "me@x.com", "script": "00_cold"})
    assert "CAN-SPAM" in r.headers["location"]

    monkeypatch.delenv("SMTP_HOST")
    r = owner.post(f"{URL}/test-email", headers=ORIGIN, data={"to_email": "me@x.com", "script": "00_cold"})
    assert "SMTP" in r.headers["location"]
    assert setup["sent"] == []


def test_test_emails_are_limited_per_day(setup, monkeypatch):
    monkeypatch.setattr(campaign_tests, "EMAIL_LIMIT_PER_DAY", 2)
    owner = client_for("owner@x.com")
    for _ in range(3):
        r = owner.post(f"{URL}/test-email", headers=ORIGIN, data={"to_email": "me@x.com", "script": "00_cold"})
    assert "last%2024%20hours" in r.headers["location"] and len(setup["sent"]) == 2


def test_only_owners_who_see_the_campaign_can_test(setup):
    db = setup["db"]
    viewer = client_for("viewer@x.com")
    assert viewer.post(f"{URL}/test-email", headers=ORIGIN, data={"to_email": "me@x.com", "script": "00_cold"}).status_code == 403
    assert start_call(viewer).status_code == 403
    assert viewer.get("/voice/token").status_code == 403

    # A campaign assigned to another Owner is invisible to this one
    other = make_user(db, "other@x.com", access.OWNER_ROLE)
    db.add_campaign_owner("voice-test", other, db.load_current_user(setup["boss"]))
    owner = client_for("owner@x.com")
    assert owner.post(f"{URL}/test-email", headers=ORIGIN, data={"to_email": "me@x.com", "script": "00_cold"}).status_code == 404
    assert start_call(owner).status_code == 404
    assert setup["sent"] == [] and rows_of(db, "call") == []


# ── Test calls ─────────────────────────────────────────────────────────


def test_owner_test_call_rings_their_number_from_the_caller_id(setup):
    db = setup["db"]
    owner = client_for("owner@x.com")
    assert owner.get("/voice/token").status_code == 200  # Owners can connect the softphone

    r = start_call(owner)
    assert r.status_code == 200 and r.json()["to"] == "+12135550199"
    test_id = r.json()["test_call_id"]
    assert owner.get(f"/voice/test-calls/{test_id}").json()["status"] == "pending"

    twiml = dial_test(setup["owner"], test_id).text
    assert f'<Dial callerId="{CALLER_ID}"' in twiml and "<Number>+12135550199</Number>" in twiml
    assert "/webhooks/voice/twilio/status" in twiml
    assert "<Dial" in dial_test(setup["owner"], test_id).text  # Twilio retrying the same call

    post_signed(client_for(), "/webhooks/voice/twilio/status",
                {"CallSid": "CT1", "DialCallStatus": "completed", "DialCallDuration": "42"})
    body = owner.get(f"/voice/test-calls/{test_id}").json()
    assert (body["status"], body["duration_seconds"]) == ("completed", 42)
    assert db.conn.execute("SELECT COUNT(*) FROM voice_calls").fetchone()[0] == 0
    assert db.conn.execute("SELECT COUNT(*) FROM audit_log WHERE action = 'voice.test_dial'").fetchone()[0] == 2
    assert client_for("boss@x.com").get(f"/voice/test-calls/{test_id}").status_code == 404  # someone else's


def test_a_test_call_is_dialed_once_by_its_owner_while_fresh(setup):
    db = setup["db"]
    test_id = start_call(client_for("owner@x.com")).json()["test_call_id"]

    # Someone else, or nobody, can't use it
    assert voice.NOT_FOUND in dial_test(setup["boss"], test_id).text
    assert voice.NOT_FOUND in post_signed(client_for(), "/webhooks/voice/twilio/dial",
                                          {"CallSid": "CT0", "test_call_id": str(test_id)}).text
    assert voice.NOT_FOUND in dial_test(setup["owner"], 999999).text

    assert "<Dial" in dial_test(setup["owner"], test_id).text
    # A second call can't reuse it
    refused = dial_test(setup["owner"], test_id, call_sid="CT2").text
    assert voice.TEST_EXPIRED in refused and "<Dial" not in refused

    # Too old to dial
    stale = start_call(client_for("owner@x.com")).json()["test_call_id"]
    db.conn.execute("UPDATE campaign_tests SET created_at = created_at - INTERVAL '1 hour' WHERE id = ?", (stale,))
    assert voice.TEST_EXPIRED in dial_test(setup["owner"], stale, call_sid="CT3").text


def test_owner_still_cannot_dial_prospects(setup):
    db = setup["db"]
    pid = db.upsert_prospect(Prospect(name="Civic Org", state="CA"))
    oid = db.upsert_outreach(pid, setup["campaign_id"])
    db.update_outreach(oid, {"contact_phone": "(213) 555-0100"})
    r = post_signed(client_for(), "/webhooks/voice/twilio/dial",
                    {"CallSid": "CA1", "From": f"client:user-{setup['owner']}", "outreach_id": str(oid)})
    assert voice.SUPER_ADMIN_ONLY in r.text and "<Dial" not in r.text


@pytest.mark.parametrize("phone, error", [
    ("555-0100", "area code"),
    ("+44 20 7946 0958", "US and Canadian"),
    (CALLER_ID, "own caller ID"),
])
def test_bad_test_call_numbers_are_refused(setup, phone, error):
    r = start_call(client_for("owner@x.com"), phone)
    assert r.status_code == 400 and error in r.json()["detail"]
    assert rows_of(setup["db"], "call") == []


def test_test_calls_need_a_caller_id_and_a_daily_allowance(setup, monkeypatch):
    db = setup["db"]
    monkeypatch.setattr(campaign_tests, "CALL_LIMIT_PER_DAY", 1)
    owner = client_for("owner@x.com")
    assert start_call(owner).status_code == 200
    assert "last 24 hours" in start_call(owner).json()["detail"]

    db.save_campaign_file("voice-test/campaign.yaml", CAMPAIGN_YAML.replace(f'caller_id: "{CALLER_ID}"', 'caller_id: ""'))
    from web import app as webapp
    webapp._invalidate_campaign_cache()
    assert "caller ID" in start_call(client_for("boss@x.com")).json()["detail"]
    assert "Call my phone" not in client_for("boss@x.com").get(URL).text
