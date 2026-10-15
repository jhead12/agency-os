"""
Scanned lists, the mail-call-email template and who may set up Lob mail, as an
AI assistant (MCP) or the console reaches them through core/tools.py.
"""

import sys
from pathlib import Path
from types import SimpleNamespace

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import access, tools  # noqa: E402
from tests.test_access import db, make_user  # noqa: E402,F401

SETTINGS = {
    "campaign_name": "Albany fall mail",
    "postcard_headline": "Albany council votes Tuesday",
    "postcard_message": "Here is what is on the agenda, and how to weigh in.",
    "email_subject": "The Albany agenda",
    "email_message": "Following up on the postcard: here is the agenda.",
    "sender_name": "Josh",
    "sender_email": "josh@u9itus.com",
    "cities": ["Albany"],
    "max_prospects": 200,
}
SCAN = """1. JANE SMITH
123 MAIN ST
ALBANY, CA 94706
(510) 555-0100

Bo Diddley, 9 Solano Ave, Albany, CA 94706
"""


def ask(db, user, name, args, confirmed=True):
    return tools.run_tool(db, user, name, args, source="mcp:Desktop", confirmed=confirmed)


def stored_channels(db, slug):
    row = db.conn.execute("SELECT content FROM campaign_files WHERE path = ?", (f"{slug}/campaign.yaml",)).fetchone()
    return yaml.safe_load(row["content"])["channels"] if row else None


def test_only_super_admins_and_people_they_allow_make_lob_campaigns(db):
    owner = db.load_current_user(make_user(db, "owner@x.com", access.OWNER_ROLE))
    boss = db.load_current_user(make_user(db, "boss@x.com", access.SUPER_ADMIN_ROLE))
    args = {"template": "mail-call-email", "settings": SETTINGS}

    preview = ask(db, owner, "campaigns_preview_template", args)
    assert preview["ok"] and "Ask a Super Admin" in preview["mail"]
    refused = ask(db, owner, "campaigns_from_template", args)
    assert not refused["ok"] and "Only a Super Admin" in refused["error"]
    assert stored_channels(db, "albany-fall-mail") is None

    # Owners can't allow themselves; only a Super Admin can, from the console.
    assert not tools.run_tool(db, owner, "lob_allow", {"email": "owner@x.com", "allow": True},
                              source="console", confirmed=True, surface="console")["ok"]
    done = tools.run_tool(db, boss, "lob_allow", {"email": "owner@x.com", "allow": True},
                          source="console", confirmed=True, surface="console")
    assert done["ok"] and db.lob_allowed(owner.id)

    assert "you're allowed" in ask(db, owner, "campaigns_preview_template", args)["mail"]
    made = ask(db, owner, "campaigns_from_template", args)
    assert made["ok"] and made["campaign"] == "albany-fall-mail"
    assert "lob_direct_mail" in stored_channels(db, "albany-fall-mail")

    tools.run_tool(db, boss, "lob_allow", {"email": "owner@x.com", "allow": False},
                   source="console", confirmed=True, surface="console")
    assert not db.lob_allowed(owner.id)


def test_lob_permission_never_reaches_agent_accounts(db):
    boss = db.load_current_user(make_user(db, "boss@x.com", access.SUPER_ADMIN_ROLE))
    agent_id = make_user(db, "bot@x.com", access.OWNER_ROLE)
    db.conn.execute("UPDATE users SET is_agent = 1 WHERE id = ?", (agent_id,))
    refused = tools.run_tool(db, boss, "lob_allow", {"email": "bot@x.com", "allow": True},
                             source="console", confirmed=True, surface="console")
    assert not refused["ok"] and "agent" in refused["error"]
    agent = SimpleNamespace(is_super_admin=False, is_agent=True)
    assert access.channel_change_problem(agent, [], ["lob_direct_mail"], lob_allowed=True)


def test_typed_settings_are_checked_like_template_params(db):
    owner = db.load_current_user(make_user(db, "owner@x.com", access.OWNER_ROLE))
    bad = ask(db, owner, "campaigns_preview_template",
              {"template": "mail-call-email", "settings": {**SETTINGS, "max_prospects": 999_999}})
    assert not bad["ok"] and "at most" in bad["error"]
    nested = ask(db, owner, "campaigns_preview_template",
                 {"template": "mail-call-email", "settings": {**SETTINGS, "cities": {"a": 1}}})
    assert not nested["ok"]


def test_scanned_lists_preview_then_import_without_duplicates(db, monkeypatch):
    monkeypatch.setattr(tools, "campaign_source", lambda: [SimpleNamespace(
        db_name="albany-fall-mail", name="Albany fall mail", product="none", channels=["lob_direct_mail"],
        config_dir="x", requires_permission="")])
    owner = db.load_current_user(make_user(db, "owner@x.com", access.OWNER_ROLE))

    preview = ask(db, owner, "preview_scanned_list", {"text": SCAN})
    assert preview["found"] == 2 and preview["with_phone"] == 1
    assert preview["first"][0]["name"] == "Jane Smith" and preview["first"][0]["city"] == "Albany"

    args = {"campaign": "albany-fall-mail", "text": SCAN}
    assert ask(db, owner, "import_scanned_list", args, confirmed=False).get("needs_confirmation")
    first = ask(db, owner, "import_scanned_list", args)
    assert (first["added"], first["updated"]) == (2, 0)
    again = ask(db, owner, "import_scanned_list", args)
    assert (again["added"], again["updated"]) == (0, 2)

    empty = ask(db, owner, "import_scanned_list", {"campaign": "albany-fall-mail", "text": "no addresses here"})
    assert not empty["ok"] and "No names and addresses" in empty["error"]
