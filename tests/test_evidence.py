"""
Evidence webhooks (core/evidence.py): Lob returned mail, Smartlead and other
bounces, recorded as contact events for the lead guarantee.

Run: TEST_DATABASE_URL=postgresql://localhost/agency_os_test python -m pytest tests/test_evidence.py
"""

import hashlib
import hmac
import json
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import evidence, verify  # noqa: E402
from core.models import Prospect, SendResult  # noqa: E402
from tests.test_access import client_for, db  # noqa: E402,F401
from tests.test_lead_packages import buyer, campaign, package, provider, x402_env  # noqa: E402,F401
from tests.fake_x402_provider import FakePayer  # noqa: E402

SECRET = "whsec_test"
KEY = "hook-key-123"


@pytest.fixture
def hooks(monkeypatch):
    monkeypatch.setenv("LOB_WEBHOOK_SECRET", SECRET)
    monkeypatch.setenv("AGENCY_OS_WEBHOOK_KEY", KEY)


def contact(db, email="pat@civic.example", name="Civic Org", piece="psc_abc123"):
    campaign_id = db.upsert_campaign("ev-test", "x")
    prospect_id = db.upsert_prospect(Prospect(name=name, state="CA", address="1 Main St"))
    outreach_id = db.upsert_outreach(prospect_id, campaign_id)
    db.update_outreach(outreach_id, {"contact_email": email})
    db.log_email(outreach_id, campaign_id, "postcard", "", "", SendResult(status="sent", provider_message_id=piece))
    return prospect_id


def events(db, prospect_id):
    return [r["kind"] for r in db.conn.execute(
        "SELECT kind FROM contact_events WHERE prospect_id = ? ORDER BY id", (prospect_id,)).fetchall()]


def signed(body: bytes, stamp=None, secret=SECRET):
    stamp = str(int(time.time()) if stamp is None else stamp)
    sig = hmac.new(secret.encode(), stamp.encode() + b"." + body, hashlib.sha256).hexdigest()
    return {"Lob-Signature": sig, "Lob-Signature-Timestamp": stamp, "Content-Type": "application/json"}


def lob_event(kind="postcard.returned_to_sender", piece="psc_abc123", event_id="evt_1"):
    return json.dumps({"id": event_id, "object": "event", "reference_id": piece,
                       "event_type": {"id": kind, "resource": "postcards"}, "body": {"id": piece}}).encode()


def test_lob_signature(hooks):
    body = b'{"a": 1}'
    now = time.time()
    good = signed(body, int(now))
    assert evidence.lob_signature_ok(body, good["Lob-Signature"], good["Lob-Signature-Timestamp"], now=now)
    ms = signed(body, int(now * 1000))
    assert evidence.lob_signature_ok(body, ms["Lob-Signature"], ms["Lob-Signature-Timestamp"], now=now)
    stale = signed(body, int(now) - 600)
    assert not evidence.lob_signature_ok(body, stale["Lob-Signature"], stale["Lob-Signature-Timestamp"], now=now)
    wrong = signed(body, int(now), secret="other")
    assert not evidence.lob_signature_ok(body, wrong["Lob-Signature"], wrong["Lob-Signature-Timestamp"], now=now)
    assert not evidence.lob_signature_ok(b'{"a": 2}', good["Lob-Signature"], good["Lob-Signature-Timestamp"], now=now)


def test_lob_returned_mail(db, hooks):
    pid = contact(db)
    client = client_for()
    body = lob_event()
    assert client.post("/webhooks/lob", content=body, headers={"Content-Type": "application/json"}).status_code == 401
    first = client.post("/webhooks/lob", content=body, headers=signed(body))
    assert first.json() == {"ok": True, "recorded": 1}
    assert client.post("/webhooks/lob", content=body, headers=signed(body)).json()["recorded"] == 0  # same event
    for ignored in (lob_event("postcard.in_transit", event_id="evt_2"),
                    lob_event("letter.return_envelope.returned_to_sender", event_id="evt_3"),
                    lob_event(piece="psc_unknown", event_id="evt_4")):
        assert client.post("/webhooks/lob", content=ignored, headers=signed(ignored)).json()["recorded"] == 0
    assert events(db, pid) == ["mail_returned"]


def test_lob_mail_delivered_once_per_piece(db, hooks):
    pid = contact(db)
    client = client_for()
    for i, kind in enumerate(("postcard.in_transit", "postcard.in_local_area",
                              "postcard.processed_for_delivery", "postcard.delivered"), start=1):
        body = lob_event(kind, event_id=f"evt_d{i}")
        recorded = client.post("/webhooks/lob", content=body, headers=signed(body)).json()["recorded"]
        assert recorded == (1 if kind == "postcard.processed_for_delivery" else 0), kind
    for ignored in (lob_event("letter.return_envelope.delivered", event_id="evt_d5"),
                    lob_event("letter.delivered", piece="ltr_unknown", event_id="evt_d6")):
        assert client.post("/webhooks/lob", content=ignored, headers=signed(ignored)).json()["recorded"] == 0
    row = db.conn.execute("SELECT kind, detail FROM contact_events WHERE prospect_id = ?", (pid,)).fetchone()
    assert row["kind"] == "mail_delivered"
    assert json.loads(row["detail"])["piece_id"] == "psc_abc123"


def test_webhooks_are_off_without_secrets(db, monkeypatch):
    monkeypatch.delenv("LOB_WEBHOOK_SECRET", raising=False)
    monkeypatch.delenv("AGENCY_OS_WEBHOOK_KEY", raising=False)
    client = client_for()
    for path in ("/webhooks/lob", "/webhooks/smartlead?key=x", "/webhooks/bounce?key=x"):
        assert client.post(path, json={}).status_code == 404


def test_smartlead_and_generic_bounces(db, hooks):
    pid = contact(db)
    other = contact(db, email="someone@else.example", name="Other Org", piece="psc_2")
    client = client_for()
    bounce = {"event_type": "EMAIL_BOUNCE", "to_email": "PAT@civic.example", "event_id": "sl_1",
              "campaign_name": "Fall push"}
    assert client.post("/webhooks/smartlead?key=wrong", json=bounce).status_code == 401
    assert client.post(f"/webhooks/smartlead?key={KEY}", json=bounce).json()["recorded"] == 1
    assert client.post(f"/webhooks/smartlead?key={KEY}", json=bounce).json()["recorded"] == 0  # deduplicated
    soft = {**bounce, "event_id": "sl_2", "bounce_type": "SOFT"}
    assert client.post(f"/webhooks/smartlead?key={KEY}", json=soft).json()["recorded"] == 0
    opened = {**bounce, "event_id": "sl_3", "event_type": "EMAIL_OPEN"}
    assert client.post(f"/webhooks/smartlead?key={KEY}", json=opened).json()["recorded"] == 0
    assert events(db, pid) == ["email_bounced"] and events(db, other) == []

    generic = client.post(f"/webhooks/bounce?key={KEY}", json={"email": "someone@else.example", "type": "hard"})
    assert generic.json()["recorded"] == 1 and events(db, other) == ["email_bounced"]
    assert client.post(f"/webhooks/bounce?key={KEY}", content=b"nope").status_code == 400
    assert client.post(f"/webhooks/bounce?key={KEY}", content=b"{" + b" " * 300_000 + b"}").status_code == 413
    # The bounced address now prompts for a new email on the prospect page.
    assert verify.bounced_emails(db, pid) == {"pat@civic.example"}


def test_webhook_bounce_counts_for_a_package_lead(db, hooks, x402_env, provider):
    from core import lead_packages

    user = buyer(db)
    campaign_id = db.upsert_campaign("lp-test", "x")
    result = lead_packages.unlock(db, campaign(), campaign_id, user, package(provider),
                                  http=provider.client(), payer=FakePayer())
    row = db.conn.execute("SELECT prospect_id, package_email FROM lead_checks WHERE lead_package_id = ? ORDER BY id",
                          (result.lead_package_id,)).fetchone()
    client_for().post(f"/webhooks/smartlead?key={KEY}",
                      json={"event_type": "EMAIL_BOUNCE", "to_email": row["package_email"], "event_id": "sl_9"})
    check = verify.lead_verdict(db, row["prospect_id"])
    assert "Email bounced" in [s["label"] for s in check["signals"]]
