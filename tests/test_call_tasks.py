"""
Mail, then call, then email (docs/MAILER_FOLLOWUP_CALLS.md M2-M5): Lob-only
contacts, cadence steps that wait for an event, call tasks and the Calls due page.

Run: TEST_DATABASE_URL=postgresql://localhost/agency_os_test python -m pytest tests/test_call_tasks.py
"""

import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import web.app as webapp  # noqa: E402
from core import calls, evidence  # noqa: E402
from core.campaign import CadenceStep, CampaignConfig  # noqa: E402
from core.db import Database  # noqa: E402
from core.models import CallLog, Prospect  # noqa: E402
from core.pipeline import Pipeline  # noqa: E402
from core.registry import PluginRegistry  # noqa: E402
from tests.test_access import client_for, db, make_user  # noqa: E402,F401
from tests.test_compliance import RecordingChannel  # noqa: E402
from tests.test_lob_templates import lob  # noqa: E402,F401

CADENCE = [
    CadenceStep(touch=0, delay_days=0, script="mail_00", next_stage="contacted", channels=["lob_direct_mail"]),
    CadenceStep(touch=1, delay_days=2, script="phone_00", next_stage="contacted",
                channels=["call", "email_smtp"], after="mail_delivered", fallback_days=10),
    CadenceStep(touch=2, delay_days=3, script="01_after", next_stage="contacted",
                channels=["email_smtp"], after="call_logged"),
]


@pytest.fixture
def setup(pg_url, tmp_path, lob):  # noqa: F811
    scripts = tmp_path / "campaign" / "scripts"
    scripts.mkdir(parents=True)
    (scripts / "mail_00.yaml").write_text("key: mail_00\nmail_type: postcard\nfront: Hi\nback: Bye\n")
    (scripts / "phone_00.yaml").write_text("key: phone_00\ntitle: Did it arrive?\nbody: Hi\n")
    (scripts / "01_after.yaml").write_text("key: after\nsubject: After\nbody: Thanks for talking\n")
    campaign = CampaignConfig(name="Hybrid", product="none", prospect_sources=[],
                              channels=["email_smtp", "lob_direct_mail"], cadence=CADENCE,
                              config_dir=tmp_path / "campaign")
    database = Database(pg_url)
    campaign_id = database.upsert_campaign(campaign.db_name, str(campaign.config_dir))
    registry = PluginRegistry()
    registry.channels["lob_direct_mail"] = lob
    registry.channels["email_smtp"] = RecordingChannel("email_smtp", "email")
    return database, campaign, campaign_id, Pipeline(database, registry), registry


def outreach_for(database, campaign_id, email="ed@org.example", phone="+13105550100", address="1 Main St"):
    pid = database.upsert_prospect(Prospect(name="Eastside Org", address=address, city="LA", state="CA", zip="90001"))
    oid = database.upsert_outreach(pid, campaign_id)
    database.update_outreach(oid, {"contact_email": email, "contact_phone": phone})
    return pid, oid


def make_due(database, oid):
    database.update_outreach(oid, {"next_follow_up_at": (datetime.now() - timedelta(minutes=1)).isoformat()})


def backdate(database, sql, days, *params):
    database.conn.execute(sql, ((datetime.now() - timedelta(days=days)).isoformat(), *params))


def test_address_only_contact_gets_the_mail_step_not_an_email_step(setup):
    database, campaign, campaign_id, pipeline, registry = setup
    _, oid = outreach_for(database, campaign_id, email=None, phone=None)

    stats = pipeline.enqueue_outreach(campaign)
    assert stats["sent"] == 1 and len(registry.channels["lob_direct_mail"].requests) == 1
    assert database.get_outreach(oid).touch_count == 1

    email_only = CampaignConfig(name="Hybrid", product="none", prospect_sources=[], channels=["email_smtp"],
                                cadence=[CadenceStep(touch=1, delay_days=0, script="01_after",
                                                     next_stage="contacted", channels=["email_smtp"])],
                                config_dir=campaign.config_dir)
    make_due(database, oid)
    assert pipeline.enqueue_outreach(email_only)["no_contact"] == 1
    assert database.get_outreach(oid).touch_count == 1


def test_call_step_waits_for_delivery_then_opens_one_task(setup):
    database, campaign, campaign_id, pipeline, _ = setup
    _, oid = outreach_for(database, campaign_id)
    pipeline.enqueue_outreach(campaign)  # the postcard, piece psc_1

    stats = pipeline.enqueue_outreach(campaign)
    assert stats["waiting"] == 1 and stats["call_tasks"] == 0

    assert evidence.record_mail_delivered(database, "psc_1", source="lob", event_id="evt_1") == 1
    assert pipeline.enqueue_outreach(campaign)["waiting"] == 1  # delivered today; the call is 2 days later

    backdate(database, "UPDATE contact_events SET created_at = ? WHERE kind = 'mail_delivered'", 3)
    assert pipeline.enqueue_outreach(campaign)["call_tasks"] == 1
    assert pipeline.enqueue_outreach(campaign)["call_tasks"] == 0  # still the one open task
    task = calls.open_task_for(database, oid)
    assert (task["touch"], task["script_key"], task["reason"]) == (1, "phone_00", "mail_delivered")
    assert database.get_outreach(oid).touch_count == 1  # a task isn't a touch until the call is logged


def test_call_step_falls_back_when_delivery_is_never_confirmed(setup):
    database, campaign, campaign_id, pipeline, _ = setup
    _, oid = outreach_for(database, campaign_id)
    pipeline.enqueue_outreach(campaign)

    backdate(database, "UPDATE email_log SET sent_at = ? WHERE outreach_id = ?", 9, oid)
    assert pipeline.enqueue_outreach(campaign)["waiting"] == 1
    backdate(database, "UPDATE email_log SET sent_at = ? WHERE outreach_id = ?", 11, oid)
    assert pipeline.enqueue_outreach(campaign)["call_tasks"] == 1
    assert calls.open_task_for(database, oid)["reason"] == "mail_unconfirmed"


def test_returned_mail_still_makes_a_call_task(setup):
    database, campaign, campaign_id, pipeline, _ = setup
    _, oid = outreach_for(database, campaign_id)
    pipeline.enqueue_outreach(campaign)
    evidence.record_returned_mail(database, "psc_1", source="lob", event_id="evt_r")
    assert pipeline.enqueue_outreach(campaign)["call_tasks"] == 1
    assert calls.open_task_for(database, oid)["reason"] == "mail_returned"


def test_call_step_without_a_phone_sends_the_email_instead(setup):
    database, campaign, campaign_id, pipeline, registry = setup
    _, oid = outreach_for(database, campaign_id, phone=None)
    pipeline.enqueue_outreach(campaign)
    backdate(database, "UPDATE email_log SET sent_at = ? WHERE outreach_id = ?", 11, oid)

    stats = pipeline.enqueue_outreach(campaign)
    assert stats["sent"] == 1 and stats["call_tasks"] == 0
    assert len(registry.channels["email_smtp"].sent) == 1
    assert database.get_outreach(oid).touch_count == 2
    # No call to wait for, so the after-call email is due on the step's own timing
    make_due(database, oid)
    assert pipeline.enqueue_outreach(campaign)["sent"] == 1


def log(database, pid, oid, campaign_id, campaign, outcome="completed", **extra):
    call = CallLog(outreach_id=oid, campaign_id=campaign_id, prospect_id=pid, outcome=outcome, **extra)
    return calls.complete(database, call, database.log_call(call), campaign)


def test_logging_the_call_counts_the_touch_and_the_next_step_waits_for_it(setup):
    database, campaign, campaign_id, pipeline, registry = setup
    pid, oid = outreach_for(database, campaign_id)
    pipeline.enqueue_outreach(campaign)
    evidence.record_mail_delivered(database, "psc_1", source="lob", event_id="evt_1")
    backdate(database, "UPDATE contact_events SET created_at = ? WHERE kind = 'mail_delivered'", 3)
    pipeline.enqueue_outreach(campaign)

    task = log(database, pid, oid, campaign_id, campaign, interest_level="medium")
    assert task["touch"] == 1 and calls.open_task_for(database, oid) is None
    outreach = database.get_outreach(oid)
    assert outreach.touch_count == 2 and outreach.next_follow_up_at > datetime.now() + timedelta(days=1)

    make_due(database, oid)
    assert pipeline.enqueue_outreach(campaign)["waiting"] == 1  # 3 days after the call
    backdate(database, "UPDATE call_tasks SET done_at = ? WHERE outreach_id = ?", 4, oid)
    assert pipeline.enqueue_outreach(campaign)["sent"] == 1
    assert registry.channels["email_smtp"].sent[-1][1].startswith("Thanks for talking")

    # A call with no open task changes nothing in the cadence
    assert log(database, pid, oid, campaign_id, campaign) is None
    assert database.get_outreach(oid).touch_count == 3


def test_wrong_number_ends_the_sequence(setup):
    database, campaign, campaign_id, pipeline, _ = setup
    pid, oid = outreach_for(database, campaign_id)
    calls.open_task(database, outreach_id=oid, campaign_id=campaign_id, touch=1, script_key="phone_00")
    log(database, pid, oid, campaign_id, campaign, outcome="wrong_number")
    assert database.get_outreach(oid).stage == "closed_lost"


def test_campaign_rejects_an_unknown_after_event(tmp_path):
    (tmp_path / "campaign.yaml").write_text(yaml.dump({
        "name": "X", "product": "none", "prospect_sources": [], "channels": [],
        "cadence": [{"touch": 0, "delay_days": 0, "script": "s", "next_stage": "contacted", "after": "opened"}]}))
    with pytest.raises(ValueError, match="after must be"):
        CampaignConfig.load(tmp_path)


def test_the_hybrid_template_campaign_loads_with_every_script():
    campaign = CampaignConfig.load(Path(__file__).resolve().parent.parent / "campaigns" / "u9itus-hybrid-local")
    assert campaign.prospect_sources == ["u9itus"]
    assert [s.after for s in campaign.cadence] == ["", "mail_delivered", "call_logged", ""]
    for step in campaign.cadence:
        assert campaign.load_script(step.script)


# ── Calls due page ─────────────────────────────────────────────────────


def test_calls_due_shows_tasks_in_campaigns_the_caller_sees(db):  # noqa: F811
    make_user(db, "caller@x.com", "Caller")
    make_user(db, "viewer@x.com", "Viewer")
    configs = {c.name: c for c in webapp.get_campaigns()}
    hybrid = configs["u9itus Hybrid — Local Businesses & Organizations"]
    recruiting = next(c for c in configs.values() if c.requires_permission == "recruiting.view")
    tasks = {}
    for config, name in ((hybrid, "Visible Bakery"), (recruiting, "Hidden Law Firm")):
        campaign_id = db.upsert_campaign(config.db_name, "x")
        pid = db.upsert_prospect(Prospect(name=name, state="CA"))
        oid = db.upsert_outreach(pid, campaign_id)
        db.update_outreach(oid, {"contact_phone": "+13105550100", "touch_count": 1})
        calls.open_task(db, outreach_id=oid, campaign_id=campaign_id, touch=1,
                        script_key="phone_00_mailer_followup", reason="mail_delivered",
                        due_at=datetime.now() - timedelta(hours=1))
        tasks[name] = (pid, oid, campaign_id)

    assert client_for("viewer@x.com").get("/calls/due").status_code == 403
    caller = client_for("caller@x.com")
    page = caller.get("/calls/due").text
    assert "Visible Bakery" in page and "Hidden Law Firm" not in page
    assert "Mailer Follow-Up" in page and "Mailer delivered" in page

    pid, oid, campaign_id = tasks["Visible Bakery"]
    r = caller.post("/call-log/record", data={"prospect_id": pid, "outreach_id": oid, "campaign_id": campaign_id,
                                              "outcome": "voicemail", "return_to": "calls-due"})
    assert r.status_code == 303 and r.headers["location"] == "/calls/due"
    assert calls.open_task_for(db, oid) is None and db.get_outreach(oid).touch_count == 2
    assert "Visible Bakery" not in caller.get("/calls/due").text
