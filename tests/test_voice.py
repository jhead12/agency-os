"""
Browser calling, V1 (core/voice.py, plugins/voice/, docs/BROWSER_CALLING.md):
who may dial, which number, from which caller ID, and the consent record.

Run: TEST_DATABASE_URL=postgresql://localhost/agency_os_test python -m pytest tests/test_voice.py
"""

import base64
import hashlib
import hmac
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import access, voice  # noqa: E402
from core.models import Prospect  # noqa: E402
from plugins.voice.twilio_voice import TwilioVoice  # noqa: E402
from tests.test_access import client_for, db, make_user  # noqa: E402,F401

BASE = "https://aos.example.com"
CALLER_ID = "+13105550000"
CAMPAIGN_YAML = f"""
name: Voice Test
product: lead_list
prospect_sources: [npi_registry]
channels: [manual]
voice:
  caller_id: "{CALLER_ID}"
  company_name: Acme Outreach
"""


@pytest.fixture
def twilio_env(monkeypatch):
    for name, value in {
        "TWILIO_ACCOUNT_SID": "AC123", "TWILIO_AUTH_TOKEN": "auth-token",
        "TWILIO_API_KEY_SID": "SK123", "TWILIO_API_KEY_SECRET": "key-secret",
        "TWILIO_TWIML_APP_SID": "AP123", "AGENCY_OS_BASE_URL": BASE,
    }.items():
        monkeypatch.setenv(name, value)


def sign(path: str, params: dict, token: str = "auth-token") -> dict:
    data = BASE + path + "".join(f"{k}{params[k]}" for k in sorted(params))
    digest = hmac.new(token.encode(), data.encode(), hashlib.sha1).digest()
    return {"X-Twilio-Signature": base64.b64encode(digest).decode()}


def post_signed(client, path: str, params: dict, token: str = "auth-token"):
    return client.post(path, data=params, headers=sign(path, params, token))


@pytest.fixture
def setup(db, twilio_env):
    """A campaign with a caller ID, a Super Admin, an Owner, and an outreach to call."""
    db.save_campaign_file("voice-test/campaign.yaml", CAMPAIGN_YAML)
    campaign_id = db.upsert_campaign("voice-test", "x")
    boss = make_user(db, "boss@x.com", access.SUPER_ADMIN_ROLE)
    owner = make_user(db, "owner@x.com", access.OWNER_ROLE)

    def outreach(state="CA", phone="(213) 555-0100", name="Civic Org"):
        pid = db.upsert_prospect(Prospect(name=name, state=state, city="Los Angeles"))
        oid = db.upsert_outreach(pid, campaign_id)
        db.update_outreach(oid, {"contact_name": "Dana Lee", "contact_phone": phone})
        return pid, oid

    return {"db": db, "boss": boss, "owner": owner, "outreach": outreach, "campaign_id": campaign_id}


def dial(client, user_id, outreach_id, call_sid="CA1"):
    return post_signed(client, "/webhooks/voice/twilio/dial",
                       {"CallSid": call_sid, "From": f"client:user-{user_id}", "outreach_id": str(outreach_id)})


def call_rows(db):
    return db.conn.execute("SELECT * FROM voice_calls ORDER BY id").fetchall()


# ── Rules that don't need a database ───────────────────────────────────


@pytest.mark.parametrize("phone, number", [
    ("(213) 555-0100", "+12135550100"),
    ("1-213-555-0100", "+12135550100"),
    ("+44 20 7946 0958", "+442079460958"),
    ("555-0100", ""),
    (None, ""),
])
def test_e164(phone, number):
    assert voice.e164(phone) == number


@pytest.mark.parametrize("state, code, all_party, label", [
    ("CA", "CA", True, "CA: all-party consent"),
    ("ca ", "CA", True, "CA: all-party consent"),
    ("TX", "TX", False, "TX: one-party consent"),
    ("", None, True, "State unknown: treat as all-party consent"),
    ("California", None, True, "State unknown: treat as all-party consent"),
])
def test_consent_rule_and_label(state, code, all_party, label):
    assert voice.consent_rule(state) == (code, all_party)
    assert voice.state_label(state) == label


def test_disclosure_text_and_version():
    text = voice.disclosure_text("Acme Outreach", "Dana Lee")
    assert text.startswith("Hi, this is Dana calling from Acme Outreach.")
    assert "being recorded" in text
    assert voice.CURRENT_DISCLOSURE_VERSION == voice.disclosure_version(voice.DISCLOSURES[-1])
    assert len(voice.CURRENT_DISCLOSURE_VERSION) == 12


def test_core_voice_does_not_import_a_provider():
    source = Path(voice.__file__).read_text()
    imports = [line for line in source.splitlines() if line.startswith(("import ", "from "))]
    assert not any("twilio" in line.lower() or "plugins" in line for line in imports)


def test_twilio_access_token(twilio_env):
    token = TwilioVoice().access_token("user-7", 3600, now=1_000_000)
    header_b64, payload_b64, sig_b64 = token.split(".")
    pad = lambda s: s + "=" * (-len(s) % 4)  # noqa: E731
    header = json.loads(base64.urlsafe_b64decode(pad(header_b64)))
    payload = json.loads(base64.urlsafe_b64decode(pad(payload_b64)))
    assert header == {"typ": "JWT", "alg": "HS256", "cty": "twilio-fpa;v=1"}
    assert payload["iss"] == "SK123" and payload["sub"] == "AC123" and payload["exp"] == 1_003_600
    assert payload["grants"] == {"identity": "user-7",
                                 "voice": {"outgoing": {"application_sid": "AP123"}}}
    expected = hmac.new(b"key-secret", f"{header_b64}.{payload_b64}".encode(), hashlib.sha256).digest()
    assert base64.urlsafe_b64decode(pad(sig_b64)) == expected


def test_twilio_signature(twilio_env):
    params = {"CallSid": "CA1", "From": "client:user-1"}
    url = BASE + "/webhooks/voice/twilio/dial"
    headers = {k.lower(): v for k, v in sign("/webhooks/voice/twilio/dial", params).items()}
    assert TwilioVoice().verify_webhook(url, params, headers)
    assert not TwilioVoice().verify_webhook(url, {**params, "From": "client:user-2"}, headers)
    assert not TwilioVoice().verify_webhook(url, params, {})


def test_twilio_dial_escapes_xml():
    twiml = TwilioVoice().dial_response("+12135550100", CALLER_ID, f"{BASE}/s?a=1&b=2")
    assert f'callerId="{CALLER_ID}"' in twiml and "a=1&amp;b=2" in twiml
    assert "<Number>+12135550100</Number>" in twiml
    assert "<Say>Tom &amp; Jerry</Say><Hangup/>" in TwilioVoice().refuse_response("Tom & Jerry")


# ── Placing calls ──────────────────────────────────────────────────────


def test_unsigned_or_wrongly_signed_webhooks_are_refused(setup):
    _, oid = setup["outreach"]()
    client = client_for()
    params = {"CallSid": "CA1", "From": f"client:user-{setup['boss']}", "outreach_id": str(oid)}
    assert client.post("/webhooks/voice/twilio/dial", data=params).status_code == 401
    assert post_signed(client, "/webhooks/voice/twilio/dial", params, token="wrong").status_code == 401
    assert client.post("/webhooks/voice/twilio/status", data={"CallSid": "CA1"}).status_code == 401
    assert call_rows(setup["db"]) == []


def test_webhooks_404_when_voice_is_not_set_up(setup, monkeypatch):
    monkeypatch.delenv("TWILIO_TWIML_APP_SID")
    _, oid = setup["outreach"]()
    assert dial(client_for(), setup["boss"], oid).status_code == 404
    assert client_for("boss@x.com").get("/voice/token").status_code == 404


def test_super_admin_dials_the_outreach_number_from_the_campaign_caller_id(setup):
    db = setup["db"]
    _, oid = setup["outreach"](state="CA")
    r = dial(client_for(), setup["boss"], oid)
    assert r.status_code == 200 and r.headers["content-type"].startswith("application/xml")
    assert f'<Dial callerId="{CALLER_ID}"' in r.text
    assert "<Number>+12135550100</Number>" in r.text
    assert f'action="{BASE}/webhooks/voice/twilio/status"' in r.text

    [row] = call_rows(db)
    assert (row["provider"], row["call_sid"], row["outreach_id"], row["user_id"]) == ("twilio", "CA1", oid, setup["boss"])
    assert (row["caller_id"], row["to_number"], row["status"]) == (CALLER_ID, "+12135550100", "initiated")
    assert (row["prospect_state"], row["all_party_consent"]) == ("CA", 1)
    assert row["disclosure_version"] == voice.CURRENT_DISCLOSURE_VERSION
    assert row["disclosure_read_at"] is None

    # Twilio retrying the same request gets the same answer and no second row
    assert "<Dial" in dial(client_for(), setup["boss"], oid).text
    assert len(call_rows(db)) == 1
    assert db.conn.execute("SELECT COUNT(*) FROM audit_log WHERE action = 'voice.dial'").fetchone()[0] >= 1


@pytest.mark.parametrize("state, stored, all_party", [("TX", "TX", 0), ("", None, 1)])
def test_consent_state_is_recorded(setup, state, stored, all_party):
    _, oid = setup["outreach"](state=state)
    dial(client_for(), setup["boss"], oid)
    [row] = call_rows(setup["db"])
    assert (row["prospect_state"], row["all_party_consent"]) == (stored, all_party)


def test_refusals_hang_up_and_record_nothing(setup):
    db = setup["db"]
    client = client_for()
    _, oid = setup["outreach"]()

    # Not a Super Admin (an Owner), or no identity at all
    r = dial(client, setup["owner"], oid)
    assert voice.SUPER_ADMIN_ONLY in r.text and "<Hangup/>" in r.text and "<Dial" not in r.text
    assert voice.SUPER_ADMIN_ONLY in post_signed(client, "/webhooks/voice/twilio/dial",
                                                 {"CallSid": "CA9", "outreach_id": str(oid)}).text

    # Unknown outreach
    assert voice.NOT_FOUND in dial(client, setup["boss"], 999999, "CA2").text

    # Do not call
    pid_dnc, oid_dnc = setup["outreach"](name="Quiet Org")
    db.set_do_not_call(None, pid_dnc, True)
    assert voice.DO_NOT_CALL in dial(client, setup["boss"], oid_dnc, "CA3").text

    # No dialable number
    _, oid_short = setup["outreach"](name="Short Org", phone="555-0100")
    assert voice.NO_NUMBER in dial(client, setup["boss"], oid_short, "CA4").text

    assert call_rows(db) == []
    assert db.conn.execute("SELECT COUNT(*) FROM audit_log WHERE action = 'voice.refused'").fetchone()[0] == 5


def test_rules_hold_for_any_provider(setup):
    """place_call() never asks the provider anything: the same rules apply to a
    second calling service, and its calls are recorded under its own key."""
    from web.app import get_campaigns
    db = setup["db"]
    campaigns = get_campaigns()
    _, oid = setup["outreach"]()
    pid_dnc, oid_dnc = setup["outreach"](name="Quiet Org")
    db.set_do_not_call(None, pid_dnc, True)

    assert voice.place_call(db, campaigns, "fake", "F1", f"user-{setup['owner']}", oid) == (None, voice.SUPER_ADMIN_ONLY)
    assert voice.place_call(db, campaigns, "fake", "F2", f"user-{setup['boss']}", oid_dnc) == (None, voice.DO_NOT_CALL)
    call, refusal = voice.place_call(db, campaigns, "fake", "F3", f"user-{setup['boss']}", oid)
    assert refusal == "" and (call["provider"], call["prospect_state"], call["all_party_consent"]) == ("fake", "CA", 1)
    # The same call ID under another provider is a different call
    assert voice.place_call(db, campaigns, "twilio", "F3", f"user-{setup['boss']}", oid)[0]["id"] != call["id"]


def test_campaign_without_caller_id_is_refused(setup):
    db = setup["db"]
    db.save_campaign_file("voice-test/campaign.yaml", CAMPAIGN_YAML.replace(f'caller_id: "{CALLER_ID}"', 'caller_id: "3105550000"'))
    _, oid = setup["outreach"]()
    assert voice.NO_CALLER_ID in dial(client_for(), setup["boss"], oid).text
    assert call_rows(db) == []


def test_status_is_stored_once(setup):
    db = setup["db"]
    _, oid = setup["outreach"]()
    client = client_for()
    dial(client, setup["boss"], oid)
    status = {"CallSid": "CA1", "DialCallStatus": "completed", "DialCallDuration": "184"}
    r = post_signed(client, "/webhooks/voice/twilio/status", status)
    assert r.status_code == 200 and "<Hangup/>" in r.text
    post_signed(client, "/webhooks/voice/twilio/status", {**status, "DialCallStatus": "busy", "DialCallDuration": "0"})
    [row] = call_rows(db)
    assert (row["status"], row["duration_seconds"]) == ("completed", 184) and row["ended_at"]


# ── Token and disclosure (signed-in routes) ────────────────────────────


def test_token_is_for_super_admins_only(setup):
    r = client_for("boss@x.com").get("/voice/token")
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    body = r.json()
    assert body["identity"] == f"user-{setup['boss']}" and body["expires_in"] == voice.TOKEN_TTL_SECONDS
    assert body["token"].count(".") == 2
    assert client_for("owner@x.com").get("/voice/token").status_code == 403


def test_disclosure_read_only_by_the_rep_who_placed_the_call(setup):
    db = setup["db"]
    _, oid = setup["outreach"]()
    dial(client_for(), setup["boss"], oid)
    [row] = call_rows(db)
    origin = {"origin": "http://testserver"}

    make_user(db, "boss2@x.com", access.SUPER_ADMIN_ROLE)
    assert client_for("boss2@x.com").post(f"/voice/calls/{row['id']}/disclosure", headers=origin).status_code == 404
    assert client_for("owner@x.com").post(f"/voice/calls/{row['id']}/disclosure", headers=origin).status_code == 403
    assert call_rows(db)[0]["disclosure_read_at"] is None

    boss = client_for("boss@x.com")
    assert boss.post(f"/voice/calls/{row['id']}/disclosure", headers=origin).json() == {"ok": True}
    first = call_rows(db)[0]["disclosure_read_at"]
    assert first
    boss.post(f"/voice/calls/{row['id']}/disclosure", headers=origin)
    assert call_rows(db)[0]["disclosure_read_at"] == first  # the first confirmation stands
