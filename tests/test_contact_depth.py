"""
Contact depth tiers: what counts as mailed, emailed, phone verified,
connected and pitched, from seller histories and from our own records.

Run: python -m pytest tests/test_contact_depth.py
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import contact_depth, lead_packages  # noqa: E402
from core.contact_depth import parse_history, tier_of  # noqa: E402
from core.models import CallLog, Prospect, SendResult  # noqa: E402
from tests.fake_x402_provider import BASE, FakeProvider  # noqa: E402
from tests.test_access import client_for, db, make_user  # noqa: E402,F401


def call(outcome, **extra):
    return {"channel": "call", "outcome": outcome, **extra}


@pytest.mark.parametrize("history, stage, tier", [
    ([], "", "unworked"),
    ([{"channel": "mail", "outcome": "delivered"}], "", "mailed"),
    ([{"channel": "mail", "outcome": "returned"}], "", "unworked"),
    ([{"channel": "email", "outcome": "sent"}], "", "emailed"),
    ([{"channel": "email", "outcome": "bounced"}], "", "unworked"),
    ([{"channel": "sms", "outcome": "delivered"}], "", "unworked"),
    ([call("wrong_number"), call("disconnected"), call("no_answer"), call("busy")], "", "unworked"),
    ([call("voicemail")], "", "unworked"),
    ([call("answering_machine", org_confirmed=True)], "", "phone_verified"),
    ([call("gatekeeper")], "", "connected"),
    ([call("gatekeeper", decision_maker=True)], "", "connected"),
    ([call("completed")], "", "connected"),
    ([call("completed", decision_maker=True)], "", "pitched"),
    ([call("scheduled", pitched=True)], "", "pitched"),
    ([{"channel": "mail", "outcome": "delivered"}], "demo_scheduled", "pitched"),
])
def test_tier_is_the_deepest_step_proven(history, stage, tier):
    assert tier_of(history, stage) == tier


def test_seller_history_is_whitelisted():
    history = parse_history([
        {"channel": "call", "outcome": "completed", "decision_maker": "yes", "at": "2026-08-09"},
        {"channel": "call", "outcome": "made_up_code"},
        {"channel": "carrier_pigeon", "outcome": "delivered"},
        {"channel": "mail", "outcome": "delivered", "at": "<script>"},
        "not a dict",
    ])
    assert [h["outcome"] for h in history] == ["completed", "delivered"]
    assert history[0]["decision_maker"] is False  # only a real true counts
    assert history[1]["at"] == ""
    assert tier_of(history) == "connected"
    assert parse_history("nope") == [] and len(parse_history([{"channel": "mail", "outcome": "sent"}] * 500)) == 50


def test_seller_cannot_claim_a_deeper_tier_than_its_history():
    lead = lead_packages.parse_lead({"lead_id": "L1", "name": "Org", "tier": "pitched",
                                     "contact_history": [{"channel": "mail", "outcome": "delivered"}]})
    assert lead["tier"] == "mailed" and lead["claimed_tier"] == "pitched"


def test_catalog_tier_prices_and_guarantee_are_validated():
    good = FakeProvider().packages["p1"]
    package = lead_packages.Package.from_catalog(BASE, good)
    assert package.guaranteed and package.guarantee_tier == "phone_verified"
    assert package.royalty_for("pitched") == 1_000_000 and package.royalty_for("emailed") == 100_000
    assert package.max_royalty_atomic == 1_000_000
    for broken in ({"royalty_by_tier": {"legendary": "5"}}, {"royalty_by_tier": {"pitched": "-1"}},
                   {"royalty_by_tier": "cheap"}):
        assert lead_packages.Package.from_catalog(BASE, {**good, **broken}) is None
    no_tier = lead_packages.Package.from_catalog(BASE, {**good, "guarantee": {"verified_rate_min": 0.95}})
    assert not no_tier.guaranteed  # a 90% promise must say which contact depth it's about
    low = lead_packages.Package.from_catalog(BASE, {**good, "guarantee": {"verified_rate_min": 0.8, "tier": "mailed"}})
    assert not low.guaranteed


# ── Our own records (database) ─────────────────────────────────────────


def test_history_from_our_calls_mail_and_email(db):
    campaign_id = db.upsert_campaign("depth", "x")
    prospect_id = db.upsert_prospect(Prospect(name="Org", state="CA"))
    outreach_id = db.upsert_outreach(prospect_id, campaign_id)
    assert contact_depth.history_for_prospect(db, prospect_id) == ([], "unworked")

    db.log_email(outreach_id, campaign_id, "postcard", "", "", SendResult(status="sent", provider_message_id="psc_123"))
    assert contact_depth.history_for_prospect(db, prospect_id)[1] == "mailed"
    db.log_email(outreach_id, campaign_id, "cold", "Hi", "", SendResult(status="sent", provider_message_id="abc"))
    db.log_email(outreach_id, campaign_id, "task", "", "", SendResult(status="sent", provider_message_id="manual_1"))
    history, tier = contact_depth.history_for_prospect(db, prospect_id)
    assert tier == "emailed" and [h["channel"] for h in history] == ["mail", "email"]

    db.log_call(CallLog(outreach_id=outreach_id, campaign_id=campaign_id, prospect_id=prospect_id,
                        outcome="wrong_number"))
    assert contact_depth.history_for_prospect(db, prospect_id)[1] == "emailed"
    db.log_call(CallLog(outreach_id=outreach_id, campaign_id=campaign_id, prospect_id=prospect_id,
                        outcome="completed", decision_maker_name="Dana Director"))
    assert contact_depth.history_for_prospect(db, prospect_id)[1] == "pitched"


def test_call_log_only_accepts_known_outcomes(db):
    make_user(db, "rep@x.com", "Caller")
    campaign_id = db.upsert_campaign("depth", "x")
    prospect_id = db.upsert_prospect(Prospect(name="Org", state="CA"))
    outreach_id = db.upsert_outreach(prospect_id, campaign_id)
    form = {"prospect_id": prospect_id, "outreach_id": outreach_id, "campaign_id": campaign_id}
    client = client_for("rep@x.com")

    bad = client.post("/call-log/record", data={**form, "outcome": "made_up"})
    assert "error=" in bad.headers["location"]
    assert db.get_calls_for_prospect(prospect_id) == []

    client.post("/call-log/record", data={**form, "outcome": "disconnected"})
    [logged] = db.get_calls_for_prospect(prospect_id)
    assert logged["outcome"] == "disconnected"
    assert "Disconnected / not in service" in client.get(f"/prospects/{prospect_id}").text
