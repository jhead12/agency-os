"""
Campaign templates (core/campaign_templates.py): one JSON file renders a
campaign the app loads. The template tests need no database (a fake stands in
for campaign_files); the console tests at the end do.

Run: TEST_DATABASE_URL=postgresql://localhost/agency_os_test python -m pytest tests/test_campaign_templates.py
"""

import json
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core import access, console, workflows  # noqa: E402
from core.campaign import CampaignConfig  # noqa: E402
from core.models import SendResult  # noqa: E402
from core.campaign_templates import (  # noqa: E402
    CampaignTemplate, TemplateError, create_campaign, discover_templates,
)
from tests.test_access import db, make_user  # noqa: E402,F401

FARM = ROOT / "campaign_templates" / "farm-outreach"


class FakeDb:
    def __init__(self, files=None):
        self.files = dict(files or {})

    def campaign_files(self):
        return dict(self.files)

    def save_campaign_file(self, path, content):
        self.files[path] = content


def write(tmp_path, data, key="t"):
    d = tmp_path / key
    d.mkdir()
    (d / "template.json").write_text(json.dumps(data))
    return d


def minimal(**campaign):
    return {
        "template": {"key": "t", "params": [
            {"name": "campaign_name", "type": "text", "required": True, "default": "Test Run"},
            {"name": "kinds", "type": "multi_choice", "choices": ["a", "b"], "default": ["a"]},
        ], "kind_map": {"a": {"cats": ["x", "y"]}, "b": {"cats": ["y", "z"]}}},
        "campaign": {"name": "[[campaign_name]]", "product": "p", "prospect_sources": ["s"],
                     "channels": ["email_smtp"], "filters": {"cats": "[[kinds -> kind_map.cats]]"},
                     "cadence": [{"touch": 0, "delay_days": 0, "script": "00_hello", "next_stage": "contacted"}],
                     **campaign},
        "scripts": {"00_hello": {"key": "hello", "subject": "Hi {{org_name}}", "body": "For [[campaign_name]]"}},
    }


def test_farm_template_renders_a_loadable_campaign(tmp_path):
    template = CampaignTemplate.load(FARM)
    assert template.key == "farm-outreach" and template.status == "draft"

    slug, files = template.render({"cities": "Fresno, Visalia", "farm_types": ["orchards", "vineyards"]})
    assert slug == "farm-outreach--central-valley"
    for rel, content in files.items():
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text(content)
    config = CampaignConfig.load(tmp_path / slug)
    assert config.filters["cities"] == ["Fresno", "Visalia"]
    assert config.filters["u9itus_categories"] == ["orchard", "vineyard", "winery"]
    assert config.filters["u9itus_max"] == 250 and config.filters["state"] == "CA"
    assert [s.after for s in config.cadence] == ["", "mail_delivered", "call_logged", ""]
    postcard = config.load_script("mail_00_postcard")
    assert "{{org_name}}" in postcard["front"]  # send-time placeholders are left alone
    assert "[[" not in "".join(files.values())


def test_discover_finds_the_farm_template_and_skips_broken_ones(tmp_path, capsys):
    assert "farm-outreach" in [t.key for t in discover_templates(ROOT / "campaign_templates")]
    write(tmp_path, minimal(), "good")
    (tmp_path / "bad").mkdir()
    (tmp_path / "bad" / "template.json").write_text("{not json")
    assert [t.key for t in discover_templates(tmp_path)] == ["t"]
    assert "Failed to load campaign template bad" in capsys.readouterr().out


def test_lookup_merges_choices_in_order_and_text_fills_inline(tmp_path):
    slug, files = CampaignTemplate.load(write(tmp_path, minimal())).render({"kinds": "b,a"})
    assert slug == "test-run"
    assert yaml.safe_load(files["test-run/campaign.yaml"])["filters"]["cats"] == ["y", "z", "x"]
    assert yaml.safe_load(files["test-run/scripts/00_hello.yaml"])["body"] == "For Test Run"


@pytest.mark.parametrize("params, error", [
    ({"campaign_name": ""}, "campaign_name is required"),
    ({"kinds": "c"}, "c is not one of a, b"),
    ({"nope": 1}, "unknown params: nope"),
    ({"campaign_name": "Hi {{org_name}}"}, "may not contain"),
])
def test_bad_params_are_refused(tmp_path, params, error):
    with pytest.raises(TemplateError, match=error):
        CampaignTemplate.load(write(tmp_path, minimal())).render(params)


def test_farm_params_are_checked():
    template = CampaignTemplate.load(FARM)
    for params, error in [({"state": "California"}, "two-letter state"),
                          ({"max_prospects": "900"}, "at most 500"),
                          ({"sender_email": "joshua"}, "email address")]:
        with pytest.raises(TemplateError, match=error):
            template.render(params)


@pytest.mark.parametrize("change, error", [
    (lambda d: d["template"].pop("key"), "template.key is required"),
    (lambda d: d["campaign"]["cadence"][0].update(script="missing"), "not in scripts"),
    (lambda d: d["scripts"]["00_hello"].update(body="[[who]]"), r"\[\[who\]\] is not one of"),
    (lambda d: d["campaign"]["filters"].update(cats="[[kinds -> nomap.cats]]"), "needs template.nomap"),
    (lambda d: d["template"]["params"].append({"name": "kinds"}), "listed twice"),
])
def test_malformed_templates_are_refused(tmp_path, change, error):
    data = minimal()
    change(data)
    with pytest.raises(TemplateError, match=error):
        CampaignTemplate.load(write(tmp_path, data))


def test_a_campaign_the_app_cannot_load_fails_at_render(tmp_path):
    data = minimal()
    data["campaign"]["cadence"][0]["after"] = "someday"
    with pytest.raises(TemplateError, match="won't load"):
        CampaignTemplate.load(write(tmp_path, data)).render()


def test_create_campaign_stores_files_and_refuses_a_taken_slug(tmp_path):
    template = CampaignTemplate.load(write(tmp_path, minimal()))
    db = FakeDb()
    assert create_campaign(db, template) == "test-run"
    assert sorted(db.files) == ["test-run/campaign.yaml", "test-run/scripts/00_hello.yaml"]
    with pytest.raises(TemplateError, match="already exists"):
        create_campaign(db, template)
    with pytest.raises(TemplateError, match="already exists"):
        create_campaign(FakeDb(), template, taken=["test-run"])


# ── Console commands (core/tools.py), on a real database ───────────────


def console_db(db, monkeypatch):
    """An Owner and a Caller; no campaigns are loaded."""
    monkeypatch.setattr("core.tools.campaign_source", lambda: [])
    return (db.load_current_user(make_user(db, "owner@x.com", access.OWNER_ROLE)),
            db.load_current_user(make_user(db, "caller@x.com", "Caller")))


def test_console_lists_previews_and_makes_a_campaign_from_a_template(db, monkeypatch):
    owner, caller = console_db(db, monkeypatch)
    changed = []
    monkeypatch.setattr("core.tools.campaigns_changed", lambda: changed.append(1))

    assert "farm-outreach" in console.run_line(db, owner, "campaigns templates")["output"]
    preview = console.run_line(db, owner, "campaigns preview-template --template farm-outreach "
                                          '--param "campaign_name=Farm Outreach Fresno" --param cities=Fresno')
    assert preview["ok"] and "farm-outreach-fresno/campaign.yaml" in preview["output"]
    assert db.campaign_files() == {}  # preview saves nothing

    bad = console.run_line(db, owner, "campaigns preview-template --template farm-outreach --param state=California")
    assert not bad["ok"] and "two-letter state" in bad["output"]
    assert "no campaign template nope" in console.run_line(db, owner, "campaigns preview-template --template nope")["output"]

    line = "campaigns from-template --template farm-outreach --param cities=Fresno"
    assert console.run_line(db, owner, line).get("needs_confirmation")
    # The template mails through Lob: an Owner needs a Super Admin's go-ahead first.
    refused = console.run_line(db, owner, line, confirmed=True)
    assert not refused["ok"] and "Only a Super Admin" in refused["output"] and db.campaign_files() == {}
    db.set_lob_allowed(owner.id, True, None)
    made = console.run_line(db, owner, line, confirmed=True)
    assert made["ok"] and "farm-outreach--central-valley" in made["output"] and changed == [1]
    assert "farm-outreach--central-valley/scripts/mail_00_postcard.yaml" in db.campaign_files()
    assert "already exists" in console.run_line(db, owner, line, confirmed=True)["output"]

    assert not console.run_line(db, caller, "campaigns templates")["ok"]  # Owners only


def test_the_tutorial_runs_only_lines_that_work_and_save_nothing(db, monkeypatch):
    owner, _ = console_db(db, monkeypatch)
    tutorial = workflows.tutorial(owner, "campaign-from-a-template")
    runs = [s["run"] for s in tutorial["steps"] if "run" in s]
    assert runs
    for line in runs:
        out = console.run_line(db, owner, line)
        assert not out.get("needs_confirmation"), line
        assert out["ok"] or "two-letter state" in out["output"], (line, out["output"])
    assert db.campaign_files() == {}


def test_making_a_campaign_emails_the_creator_a_receipt(db, monkeypatch):
    owner, _ = console_db(db, monkeypatch)
    db.set_lob_allowed(owner.id, True, None)
    sent = []
    monkeypatch.setattr("core.welcome.smtp_configured", lambda: True)
    monkeypatch.setattr("core.welcome.send", lambda to, subject, body: sent.append((to, subject, body))
                        or SendResult(status="sent"))
    monkeypatch.setattr("core.tools.site_url", lambda: "https://os.example")

    made = console.run_line(db, owner, "campaigns from-template --template farm-outreach --param cities=Fresno",
                            confirmed=True)
    assert made["ok"] and "Receipt emailed to owner@x.com" in made["output"]
    [(to, subject, body)] = sent
    assert to == "owner@x.com" and subject == "Campaign made: farm-outreach--central-valley"
    assert "Nothing has been sent yet" in body and "Fresno" in body
    assert "https://os.example/admin/campaigns/farm-outreach--central-valley" in body
    assert "Lob direct mail" in body


def test_no_receipt_without_smtp_and_the_campaign_is_still_made(db, monkeypatch):
    owner, _ = console_db(db, monkeypatch)
    db.set_lob_allowed(owner.id, True, None)
    monkeypatch.setattr("core.welcome.smtp_configured", lambda: False)
    monkeypatch.setattr("core.welcome.send", lambda *a: pytest.fail("no SMTP: nothing is sent"))

    made = console.run_line(db, owner, "campaigns from-template --template farm-outreach --param cities=Fresno",
                            confirmed=True)
    assert made["ok"] and "Receipt" not in made["output"]
    assert "farm-outreach--central-valley/campaign.yaml" in db.campaign_files()
