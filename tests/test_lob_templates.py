"""
Tests for Lob HTML template designs on mail templates: the channel payload,
pipeline wiring, and the Mail Templates page.

Run: python -m pytest tests/
"""

import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import web.app as webapp  # noqa: E402
from core.campaign import CadenceStep, CampaignConfig  # noqa: E402
from core.db import Database  # noqa: E402
from core.models import Prospect  # noqa: E402
from core.pipeline import Pipeline  # noqa: E402
from core.registry import PluginRegistry  # noqa: E402
from plugins.channels.lob_direct_mail import LobDirectMailChannel, lob_template_url  # noqa: E402
from tests.test_access import client_for, db, make_user  # noqa: E402,F401

TO = {"name": "Ana", "company": "Org", "address": "1 Main St", "city": "LA", "state": "CA", "zip": "90001"}


@pytest.fixture
def lob(monkeypatch):
    for key, value in {"LOB_API_KEY": "test_key", "LOB_FROM_ADDRESS_LINE1": "2 Side St",
                       "LOB_FROM_ADDRESS_CITY": "LA", "LOB_FROM_ADDRESS_STATE": "CA",
                       "LOB_FROM_ADDRESS_ZIP": "90002"}.items():
        monkeypatch.setenv(key, value)
    channel = LobDirectMailChannel()
    channel.requests = []

    def fake_request(method, path, json=None):
        channel.requests.append((path, json))
        return {"id": "psc_1"}

    channel._request = fake_request
    return channel


def test_template_link():
    assert lob_template_url("tmpl_abc123") == "https://dashboard.lob.com/templates/tmpl_abc123"
    assert lob_template_url("") == lob_template_url("not an id") == "https://dashboard.lob.com/templates"


def test_postcard_with_designs_sends_template_ids_and_merge_variables(lob):
    result = lob.send(TO, "", "", {
        "mail_template": {"mail_type": "postcard", "front": "Hi", "back": "There",
                          "front_template_id": "tmpl_front1", "back_template_id": "tmpl_back1"},
        "variables": {"org_name": "Org", "touch": 2},
    })
    assert result.status == "sent"
    path, payload = lob.requests[0]
    assert path == "/postcards"
    assert (payload["front"], payload["back"]) == ("tmpl_front1", "tmpl_back1")
    assert payload["merge_variables"] == {"org_name": "Org", "touch": "2"}
    assert payload["to"]["company"] == "Org" and payload["to"]["address_zip"] == "90001"


def test_postcard_without_designs_sends_html(lob):
    lob.send(TO, "", "", {"mail_template": {"mail_type": "postcard", "front": "Hello Org", "back": "Body"},
                          "variables": {"org_name": "Org"}})
    payload = lob.requests[0][1]
    assert "Hello Org" in payload["front"] and "Body" in payload["back"]
    assert "merge_variables" not in payload


def test_letter_with_design(lob):
    lob.send(TO, "", "", {"mail_template": {"mail_type": "letter", "template_id": "tmpl_letter1"},
                          "variables": {"org_name": "Org"}})
    path, payload = lob.requests[0]
    assert path == "/letters" and payload["file"] == "tmpl_letter1"
    assert payload["merge_variables"] == {"org_name": "Org"}


def test_pipeline_passes_address_rendered_template_and_variables(pg_url, tmp_path, lob):
    scripts = tmp_path / "campaign" / "scripts"
    scripts.mkdir(parents=True)
    (scripts / "mail_00.yaml").write_text(yaml.dump({
        "key": "mail_00", "mail_type": "postcard", "front": "Hi {{org_name}}", "back": "Bye",
        "front_template_id": "tmpl_front1",
    }))
    campaign = CampaignConfig(
        name="Mail Campaign", product="none", prospect_sources=[], channels=["lob_direct_mail"],
        cadence=[CadenceStep(touch=0, delay_days=3, script="mail_00", next_stage="contacted")],
        config_dir=tmp_path / "campaign",
    )
    db = Database(pg_url)
    campaign_id = db.upsert_campaign(campaign.db_name, str(campaign.config_dir))
    pid = db.upsert_prospect(Prospect(name="Eastside Org", ein="1", address="1 Main St",
                                      city="LA", state="CA", zip="90001"))
    oid = db.upsert_outreach(pid, campaign_id)
    db.update_outreach(oid, {"contact_email": "ed@org.example"})
    registry = PluginRegistry()
    registry.channels["lob_direct_mail"] = lob

    Pipeline(db, registry).enqueue_outreach(campaign)

    payload = lob.requests[0][1]
    assert payload["to"]["company"] == "Eastside Org" and payload["to"]["address_line1"] == "1 Main St"
    assert payload["front"] == "tmpl_front1"
    assert "Bye" in payload["back"]
    assert payload["merge_variables"]["org_name"] == "Eastside Org"


def test_test_email_mode_never_mails_a_prospect(pg_url, tmp_path, lob):
    scripts = tmp_path / "campaign" / "scripts"
    scripts.mkdir(parents=True)
    (scripts / "mail_00.yaml").write_text("key: mail_00\nmail_type: postcard\nfront: Hi\nback: Bye\n")
    campaign = CampaignConfig(
        name="Mail Campaign", product="none", prospect_sources=[], channels=["lob_direct_mail"],
        cadence=[CadenceStep(touch=0, delay_days=3, script="mail_00", next_stage="contacted")],
        config_dir=tmp_path / "campaign",
    )
    db = Database(pg_url)
    campaign_id = db.upsert_campaign(campaign.db_name, str(campaign.config_dir))
    pid = db.upsert_prospect(Prospect(name="Org", ein="1", address="1 Main St", city="LA", state="CA", zip="90001"))
    db.update_outreach(db.upsert_outreach(pid, campaign_id), {"contact_email": "ed@org.example"})
    registry = PluginRegistry()
    registry.channels["lob_direct_mail"] = lob

    Pipeline(db, registry).enqueue_outreach(campaign, test_email="me@agency.example")

    assert lob.requests == []


def test_mail_templates_page_saves_design_ids_and_links_to_lob(db):
    make_user(db, "editor@x.com", "Template Editor")
    client = client_for("editor@x.com")
    page = client.get("/mail-templates")
    assert page.status_code == 200 and "https://dashboard.lob.com/templates" in page.text

    path = webapp.CAMPAIGNS_DIR / "voter-guide-cbo" / "scripts" / "mail_00_postcard_cold.yaml"
    r = client.post("/mail-templates/save", data={
        "file_path": str(path), "mail_type": "postcard", "front": "F", "back": "B",
        "front_template_id": " tmpl_front1 ", "back_template_id": "",
    })
    assert r.status_code == 303 and "saved=1" in r.headers["location"]
    saved = yaml.safe_load(db.campaign_files()["voter-guide-cbo/scripts/mail_00_postcard_cold.yaml"])
    assert saved["front_template_id"] == "tmpl_front1" and "back_template_id" not in saved
    assert "https://dashboard.lob.com/templates/tmpl_front1" in client.get("/mail-templates").text

    r = client.post("/mail-templates/save", data={
        "file_path": str(path), "mail_type": "postcard", "front_template_id": "<script>",
    })
    assert "error=" in r.headers["location"]
    saved = yaml.safe_load(db.campaign_files()["voter-guide-cbo/scripts/mail_00_postcard_cold.yaml"])
    assert saved["front_template_id"] == "tmpl_front1"


def test_viewer_cannot_save_designs(db):
    make_user(db, "viewer@x.com", "Viewer")
    path = webapp.CAMPAIGNS_DIR / "voter-guide-cbo" / "scripts" / "mail_00_postcard_cold.yaml"
    r = client_for("viewer@x.com").post("/mail-templates/save", data={
        "file_path": str(path), "mail_type": "postcard", "front_template_id": "tmpl_x1",
    })
    assert r.status_code in (302, 303, 403) and "saved=1" not in r.headers.get("location", "")


