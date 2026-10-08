"""CAN-SPAM: footer with unsubscribe link and postal address, the unsubscribe
page, and suppressed addresses never emailed (core/compliance.py)."""

import sys
from datetime import datetime
from email.message import EmailMessage
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import web.app as webapp  # noqa: E402
from core import compliance  # noqa: E402
from core.campaign import CadenceStep, CampaignConfig  # noqa: E402
from core.db import Database  # noqa: E402
from core.models import Prospect, SendResult  # noqa: E402
from core.pipeline import Pipeline  # noqa: E402
from core.registry import PluginRegistry  # noqa: E402
from plugins.channels.email_smtp import EmailSmtpChannel  # noqa: E402


class RecordingChannel:
    def __init__(self, key, needs):
        self.key, self.needs, self.sent = key, needs, []

    def is_configured(self):
        return True

    def send(self, recipient, subject, body, metadata):
        if not recipient.get(self.needs):
            return SendResult(status="skipped")
        self.sent.append((recipient, body, metadata))
        return SendResult(status="sent", sent_at=datetime.now())


@pytest.fixture
def env(pg_url, tmp_path):
    scripts = tmp_path / "campaign" / "scripts"
    scripts.mkdir(parents=True)
    (scripts / "00_hello.yaml").write_text("key: hello\nsubject: Hi\nbody: Hello there\n")
    campaign = CampaignConfig(
        name="Compliance", product="none", prospect_sources=[], channels=["email", "sms"],
        cadence=[CadenceStep(touch=0, delay_days=3, script="00_hello", next_stage="contacted")],
        config_dir=tmp_path / "campaign",
    )
    db = Database(pg_url)
    campaign_id = db.upsert_campaign(campaign.db_name, str(campaign.config_dir))
    registry = PluginRegistry()
    registry.channels["email"] = RecordingChannel("email", "email")
    registry.channels["sms"] = RecordingChannel("sms", "phone")

    def add(email, phone=None):
        pid = db.upsert_prospect(Prospect(name=f"Org {email}", ein=email))
        oid = db.upsert_outreach(pid, campaign_id)
        db.update_outreach(oid, {"contact_email": email, "contact_phone": phone})
        return oid

    return db, Pipeline(db, registry), registry, campaign, add


def test_footer_has_postal_address_and_a_working_link():
    body = compliance.with_footer("Hello", "Someone@Org.org")
    assert "PO Box 1, Albany, CA 94706" in body
    url = compliance.unsubscribe_url("someone@org.org")
    assert url in body and url.startswith("https://agency.test/unsubscribe?")
    q = parse_qs(urlsplit(url).query)
    assert q["e"] == ["someone@org.org"] and compliance.token_ok("SOMEONE@org.org", q["t"][0])
    assert not compliance.token_ok("other@org.org", q["t"][0])


def test_problem_names_each_missing_setting(monkeypatch):
    assert compliance.problem() is None
    monkeypatch.delenv("AGENCY_OS_POSTAL_ADDRESS")
    assert "AGENCY_OS_POSTAL_ADDRESS" in compliance.problem()


def test_every_outreach_email_carries_the_footer(env):
    db, pipeline, registry, campaign, add = env
    oid = add("x@org.org")
    assert pipeline.enqueue_outreach(campaign)["sent"] == 1
    recipient, body, metadata = registry.channels["email"].sent[0]
    assert body.startswith("Hello there") and "PO Box 1" in body
    assert metadata["unsubscribe_url"] == compliance.unsubscribe_url("x@org.org")
    assert metadata["unsubscribe_url"] in body
    logged = db.conn.execute("SELECT body FROM email_log WHERE outreach_id = ?", (oid,)).fetchone()
    assert "Unsubscribe:" in logged["body"]  # the log shows what was really sent


def test_sms_gets_no_email_footer(env):
    _db, pipeline, registry, campaign, add = env
    add("", phone="3105550123")
    pipeline.enqueue_outreach(campaign)
    assert registry.channels["sms"].sent[0][1] == "Hello there"


def test_suppressed_address_is_never_emailed(env):
    db, pipeline, registry, campaign, add = env
    add("gone@org.org")
    db.suppress_email("GONE@org.org", "unsubscribed", "link")
    stats = pipeline.enqueue_outreach(campaign)
    assert stats["suppressed"] == 1 and stats["sent"] == 0
    assert registry.channels["email"].sent == []


def test_suppression_follows_the_real_contact_in_test_mode(env):
    db, pipeline, registry, campaign, add = env
    add("gone@org.org")
    db.suppress_email("gone@org.org", "unsubscribed", "link")
    assert pipeline.enqueue_outreach(campaign, test_email="me@test.org")["suppressed"] == 1
    assert registry.channels["email"].sent == []


def test_email_channels_stay_off_without_the_settings(env, monkeypatch):
    _db, pipeline, registry, campaign, add = env
    monkeypatch.delenv("AGENCY_OS_UNSUBSCRIBE_SECRET")
    add("x@org.org")
    stats = pipeline.enqueue_outreach(campaign)
    assert stats["sent"] == 0 and registry.channels["email"].sent == []


def test_smtp_sends_one_click_unsubscribe_headers(monkeypatch):
    for k, v in {"SMTP_HOST": "smtp.test", "SMTP_USER": "u", "SMTP_PASS": "p"}.items():
        monkeypatch.setenv(k, v)
    sent: list[EmailMessage] = []
    with patch("smtplib.SMTP") as smtp:
        smtp.return_value.__enter__.return_value.send_message.side_effect = sent.append
        url = compliance.unsubscribe_url("x@org.org")
        EmailSmtpChannel().send({"email": "x@org.org"}, "Hi", "Body", {"unsubscribe_url": url})
    assert sent[0]["List-Unsubscribe"] == f"<{url}>"
    assert sent[0]["List-Unsubscribe-Post"] == "List-Unsubscribe=One-Click"


# ── The page ───────────────────────────────────────────────────────────


@pytest.fixture
def web_db(pg_url, tmp_path, monkeypatch):
    monkeypatch.setattr(webapp, "DB_URL", pg_url)
    monkeypatch.setattr(webapp, "CAMPAIGNS_DIR", tmp_path / "campaigns")
    monkeypatch.setattr(webapp, "_campaigns_synced", False)
    database = Database(pg_url)
    database.install_access()
    return database


def test_link_asks_first_then_unsubscribes(web_db):
    client = TestClient(webapp.app)
    path = compliance.unsubscribe_url("x@org.org").removeprefix("https://agency.test")
    page = client.get(path)
    assert page.status_code == 200 and "Stop all emails to" in page.text
    assert not web_db.email_suppressed("x@org.org")  # opening the link alone does nothing
    done = client.post(path)
    assert done.status_code == 200 and "won't get any more emails" in done.text
    assert web_db.email_suppressed("x@org.org")
    assert client.post(path).status_code == 200  # twice is fine


def test_one_click_post_from_a_mail_client(web_db):
    path = compliance.unsubscribe_url("x@org.org").removeprefix("https://agency.test")
    r = TestClient(webapp.app).post(path, content="List-Unsubscribe=One-Click",
                                    headers={"Content-Type": "application/x-www-form-urlencoded",
                                             "Origin": "https://mail.google.com"})
    assert r.status_code == 200 and web_db.email_suppressed("x@org.org")


def test_forged_link_is_refused(web_db):
    client = TestClient(webapp.app)
    assert client.get("/unsubscribe?e=victim@org.org&t=deadbeef").status_code == 400
    assert client.post("/unsubscribe?e=victim@org.org&t=deadbeef").status_code == 400
    assert not web_db.email_suppressed("victim@org.org")
