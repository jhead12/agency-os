"""
Super Admins, the command console (/console, /api/console), CLI keys and the
remote CLI (`agency_os.py connect` / `remote`).

Run: TEST_DATABASE_URL=postgresql://localhost/agency_os_test python -m pytest tests/test_console.py
"""

import json
import re
import sys
from pathlib import Path

import pytest
from click.testing import CliRunner
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import web.app as webapp  # noqa: E402
from core import access, console, mcp_auth, tools  # noqa: E402
from core.access import AccessError  # noqa: E402
from core.cli import cli  # noqa: E402
import core.cli as core_cli  # noqa: E402
from tests.test_access import client_for, db, make_user, role_id  # noqa: E402,F401

BASE = "https://dash.example"
HEADERS = {"X-AOS-Console": "1"}


@pytest.fixture(autouse=True)
def server_env(monkeypatch):
    monkeypatch.setenv("AGENCY_OS_BASE_URL", BASE)
    for var in ("SMTP_HOST", "SMTP_USER", "SMTP_PASS"):
        monkeypatch.delenv(var, raising=False)


def console_user(db, email="caller@x.com"):
    """A Caller who may also use the console."""
    if not any(r["name"] == "Console Caller" for r in db.list_roles()):
        perms = sorted(set(access.STARTER_ROLES["Caller"][1]) | {"cli.use"})
        db.create_role("Console Caller", "Caller + console", perms, actor=None)
    return make_user(db, email, "Console Caller")


def run(client, line, confirmed=False):
    r = client.post("/api/console", headers=HEADERS, json={"line": line, "confirmed": confirmed})
    assert r.status_code == 200, r.text
    return r.json()


# ── Super Admin rules (enforced in the database for every surface) ─────


def test_only_super_admin_grants_or_removes_owner(db):
    owner = db.load_current_user(make_user(db, "owner@x.com", access.OWNER_ROLE))
    boss = db.load_current_user(make_user(db, "boss@x.com", access.SUPER_ADMIN_ROLE))
    assert boss.is_owner and boss.is_super_admin and not owner.is_super_admin

    with pytest.raises(AccessError, match="Only a Super Admin"):
        db.create_user("new@x.com", "New", "long-enough-pw", [role_id(db, access.OWNER_ROLE)], actor=owner)
    db.create_user("new@x.com", "New", "long-enough-pw", [role_id(db, access.OWNER_ROLE)], actor=boss)

    rep = make_user(db, "rep@x.com", "Caller")
    with pytest.raises(AccessError, match="Only a Super Admin"):
        db.update_user(rep, name="rep", is_active=True, role_ids=[role_id(db, access.OWNER_ROLE)], actor=owner)
    with pytest.raises(AccessError, match="Super Admin's account"):
        db.update_user(boss.id, name="boss", is_active=False, role_ids=[], actor=owner)
    # Owners still manage everyone else.
    db.update_user(rep, name="rep", is_active=True, role_ids=[role_id(db, "Viewer")], actor=owner)


def test_owner_cannot_make_owner_in_the_web_admin(db):
    make_user(db, "owner@x.com", access.OWNER_ROLE)
    r = client_for("owner@x.com").post("/admin/users", data={
        "email": "x@x.com", "name": "X", "password": "long-enough-pw",
        "role_ids": [role_id(db, access.OWNER_ROLE)]})
    assert "error=" in r.headers["location"] and db.get_user_by_email("x@x.com") is None


def test_cli_bootstraps_a_super_admin(db):
    make_user(db, "me@x.com", "Viewer")
    r = CliRunner().invoke(cli, ["--db", webapp.DB_URL, "users", "grant-super-admin", "--email", "me@x.com"])
    assert r.exit_code == 0, r.output
    assert db.load_current_user(db.get_user_by_email("me@x.com")["id"]).is_super_admin


# ── Console access ─────────────────────────────────────────────────────


def test_console_needs_cli_use(db):
    make_user(db, "plain@x.com", "Caller")
    c = client_for("plain@x.com")
    assert c.get("/console").status_code == 403
    assert c.post("/api/console", headers=HEADERS, json={"line": "help"}).status_code == 403
    assert "/console" not in c.get("/account").text

    console_user(db)
    c = client_for("caller@x.com")
    assert c.get("/console").status_code == 200
    assert 'href="/console"' in c.get("/account").text
    assert c.post("/api/console", json={"line": "help"}).status_code == 400  # no X-AOS-Console header


def test_help_lists_only_permitted_commands(db):
    console_user(db)
    out = run(client_for("caller@x.com"), "help")["output"]
    assert "search-prospects" in out and "log-call" in out
    assert "users invite" not in out and "users create-owner" not in out

    make_user(db, "owner@x.com", access.OWNER_ROLE)
    out = run(client_for("owner@x.com"), "help")["output"]
    assert "users invite" in out and "users create-owner" not in out
    assert "needs the Super Admin role" in run(client_for("owner@x.com"), "users create-owner --email a@x.com")["output"]

    make_user(db, "boss@x.com", access.SUPER_ADMIN_ROLE)
    assert "users create-owner" in run(client_for("boss@x.com"), "help")["output"]
    assert "--email <email>" in run(client_for("boss@x.com"), "help users invite")["output"]


def test_invite_asks_first_then_creates_and_audits(db):
    make_user(db, "owner@x.com", access.OWNER_ROLE)
    c = client_for("owner@x.com")
    line = 'users invite --email Jane@x.com --name "Jane Doe" --role Caller --role Viewer --no-send'
    first = run(c, line)
    assert first["needs_confirmation"] and db.get_user_by_email("jane@x.com") is None

    done = run(c, line, confirmed=True)
    assert done["ok"], done
    assert f"link: {BASE}/welcome/" in done["output"]
    jane = db.load_current_user(db.get_user_by_email("jane@x.com")["id"])
    assert jane.name == "Jane Doe" and jane.roles == ("Caller", "Viewer")
    actions = [(r["action"], r["actor_label"]) for r in db.list_audit()]
    assert ("tool.users_invite", "owner@x.com") in actions and ("user.create", "owner@x.com") in actions

    refused = run(c, "users invite --email o@x.com --role Owner --no-send", confirmed=True)
    assert not refused["ok"] and "Only a Super Admin" in refused["output"]


def test_super_admin_creates_owner_and_sets_roles(db):
    make_user(db, "boss@x.com", access.SUPER_ADMIN_ROLE)
    c = client_for("boss@x.com")
    out = run(c, "users create-owner --email joshua@x.com --name Joshua --no-send", confirmed=True)
    assert out["ok"], out
    assert db.load_current_user(db.get_user_by_email("joshua@x.com")["id"]).roles == ("Owner",)

    out = run(c, "users set-roles --email joshua@x.com --role 'Sales Rep'", confirmed=True)
    assert out["ok"], out
    assert db.load_current_user(db.get_user_by_email("joshua@x.com")["id"]).roles == ("Sales Rep",)
    assert "joshua@x.com" in run(c, "users list")["output"]


def test_parse_errors_are_friendly(db):
    make_user(db, "owner@x.com", access.OWNER_ROLE)
    c = client_for("owner@x.com")
    assert "Unknown flag --nope" in run(c, "users invite --nope 1")["output"]
    assert "--email needs a value" in run(c, "users invite --email")["output"]
    assert "Couldn't read" in run(c, 'users invite --name "unclosed')["output"]
    assert "'rm' is a shell command" in run(c, "rm -rf /")["output"]
    assert "whole number" in run(c, "get-prospect --prospect-id abc")["output"]


def test_errors_teach_the_fix(db):
    make_user(db, "owner@x.com", access.OWNER_ROLE)
    console_user(db)
    owner = client_for("owner@x.com")
    out = lambda c, line: run(c, line)["output"]  # noqa: E731

    assert "Did you mean: search-prospects" in out(owner, "serch-prospects --q food")
    assert "Did you mean: users invite" in out(owner, "users invit --email a@x.com")
    assert "users needs a subcommand: users list, users invite, users set-roles" in out(owner, "users")

    missing = out(owner, "users invite")
    assert "users invite needs --email." in missing
    assert "Usage:   users invite --email <email> [--name <name>] [--role <role>]... [--no-send]" in missing
    assert "Example: users invite --email jane@example.com" in missing and "More:    help users invite" in missing

    assert "Did you mean --email?" in out(owner, "users invite --emial a@x.com")
    assert "Did you mean: get-prospect --prospect-id 42?" in out(owner, "get-prospect 42")
    assert "Flags start with two dashes: --prospect-id" in out(owner, "get-prospect -prospect-id 4")
    assert "--prospect-id must be a whole number" in out(owner, "get-prospect --prospect-id abc")
    assert "--stage must be one of: cold" in out(owner, "set-stage --prospect-id 1 --stage hot")

    not_found = out(owner, "get-prospect --prospect-id 99999999")
    assert "Prospect not found" in not_found and 'search-prospects --q "<name>"' in not_found

    assert "Ask a Super Admin" in out(owner, "users create-owner --email a@x.com")
    caller = client_for("caller@x.com")
    assert "users invite needs the Owner role. You have: Console Caller" in out(caller, "users invite --email a@x.com")
    assert "the users commands need the Owner role" in out(caller, "users")
    # A hint only names commands the user can run.
    assert "users list" not in out(caller, "log-call --prospect-id 99999999 --outcome completed")

    page = out(owner, "help set-stage")
    assert "Usage:   set-stage --prospect-id <id> --stage <stage>" in page and "Example: set-stage" in page


def test_admin_tools_stay_off_ai_surfaces(db):
    boss = db.load_current_user(make_user(db, "boss@x.com", access.SUPER_ADMIN_ROLE))
    ai_names = {t.name for t in tools.available(boss)}
    assert not {"users_list", "users_invite", "users_create_owner", "users_set_roles"} & ai_names
    assert tools.run_tool(db, boss, "users_list", {}, source="webmcp")["ok"] is False


# ── CLI keys and the remote CLI ────────────────────────────────────────


def test_cli_key_works_only_on_the_console(db):
    user_id = console_user(db)
    page = client_for("caller@x.com").post("/account/cli-keys", data={"name": "laptop"})
    key = re.search(r"aos_cli_[A-Za-z0-9_-]{20,}", page.text).group(0)

    bare = TestClient(webapp.app, follow_redirects=False)
    auth = {"Authorization": f"Bearer {key}"}
    r = bare.post("/api/console", headers=auth, json={"line": "whoami"})
    assert r.status_code == 200 and "caller@x.com" in r.json()["output"]
    assert bare.get("/console", headers=auth).status_code == 303  # pages still need a session
    assert bare.get("/api/tools", headers=auth).status_code == 401

    # An MCP personal key is not a CLI key.
    user = db.load_current_user(user_id)
    pat = mcp_auth.create_personal_token(db, user, "mcp", allow_writes=True)
    assert bare.post("/api/console", headers={"Authorization": f"Bearer {pat}"},
                     json={"line": "whoami"}).status_code == 401

    key_id = mcp_auth.list_cli_keys(db, user_id)[0]["id"]
    assert mcp_auth.revoke(db, user, key_id)
    assert bare.post("/api/console", headers=auth, json={"line": "whoami"}).status_code == 401


def test_remote_cli_connects_and_runs(db, tmp_path, monkeypatch):
    make_user(db, "owner@x.com", access.OWNER_ROLE)
    owner = db.load_current_user(db.get_user_by_email("owner@x.com")["id"])
    key = mcp_auth.create_cli_key(db, owner, "test")
    http = TestClient(webapp.app, follow_redirects=False)
    sent = []

    def fake_call(url, k, line, confirmed=False):
        sent.append((line, confirmed))
        r = http.post("/api/console", headers={"Authorization": f"Bearer {k}"},
                      json={"line": line, "confirmed": confirmed})
        return r.json() if r.status_code == 200 else {"ok": False, "output": f"HTTP {r.status_code}"}

    monkeypatch.setattr(core_cli, "_remote_call", fake_call)
    monkeypatch.setattr(core_cli, "REMOTE_CONFIG", tmp_path / "cli.json")
    for var in ("AGENCY_OS_URL", "AGENCY_OS_KEY"):
        monkeypatch.delenv(var, raising=False)

    r = CliRunner().invoke(cli, ["connect", "--url", "https://dash.example/", "--key", key])
    assert r.exit_code == 0 and "owner@x.com" in r.output, r.output
    saved = tmp_path / "cli.json"
    assert json.loads(saved.read_text()) == {"url": "https://dash.example", "key": key}
    assert saved.stat().st_mode & 0o077 == 0

    r = CliRunner().invoke(cli, ["remote", "users", "invite", "--email", "amy@x.com", "--name", "Amy Lee",
                                 "--role", "Caller", "--no-send"], input="n\n")
    assert "Cancelled" in r.output and db.get_user_by_email("amy@x.com") is None

    r = CliRunner().invoke(cli, ["remote", "--yes", "users", "invite", "--email", "amy@x.com",
                                 "--name", "Amy Lee", "--role", "Caller", "--no-send"])
    assert r.exit_code == 0, r.output
    assert db.load_current_user(db.get_user_by_email("amy@x.com")["id"]).name == "Amy Lee"
    assert sent[-1] == ("users invite --email amy@x.com --name 'Amy Lee' --role Caller --no-send", True)

    r = CliRunner().invoke(cli, ["remote", "users", "create-owner", "--email", "z@x.com"])
    assert r.exit_code == 1 and "needs the Super Admin role" in r.output


def test_format_result_tables():
    text = console.format_result({"ok": True, "users": [{"email": "a@x.com", "active": True}], "note": "hi"})
    assert text.splitlines()[:3] == ["users:", "email    active", "-------  ------"]
    assert "note: hi" in text
