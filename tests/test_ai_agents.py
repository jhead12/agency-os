"""
Owners vs other Owners, AI agent accounts, and detecting autonomous use.

Run: TEST_DATABASE_URL=postgresql://localhost/agency_os_test python -m pytest tests/test_ai_agents.py
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import access, tools  # noqa: E402
from core.access import AccessError  # noqa: E402
from tests.test_access import client_for, db, make_user, role_id  # noqa: E402,F401


def owners(db):
    me = make_user(db, "owner@x.com", access.OWNER_ROLE)
    other = make_user(db, "other@x.com", access.OWNER_ROLE)
    boss = make_user(db, "boss@x.com", access.SUPER_ADMIN_ROLE)
    return me, other, boss


def save(email, user_id, **data):
    data.setdefault("name", "n")
    return client_for(email).post(f"/admin/users/{user_id}", data=data).headers["location"]


# ── One Owner can't take over another ──────────────────────────────────


def test_owner_cannot_reset_or_deactivate_another_owner(db):
    me, other, _ = owners(db)
    owner_role = role_id(db, access.OWNER_ROLE)
    assert "error=" in save("owner@x.com", other, is_active="1", role_ids=[owner_role],
                            new_password="hijacked-password-1")
    assert "error=" in save("owner@x.com", other, role_ids=[owner_role])
    assert db.load_current_user(other) is not None
    with pytest.raises(AccessError, match="another Owner"):
        db.set_password(other, "hijacked-password-1", db.load_current_user(me))


def test_owner_still_edits_self_and_staff_and_super_admin_edits_owners(db):
    me, other, _ = owners(db)
    owner_role = role_id(db, access.OWNER_ROLE)
    rep = make_user(db, "rep@x.com", "Caller")
    assert "msg=" in save("owner@x.com", rep, is_active="1", role_ids=[role_id(db, "Viewer")])
    assert "msg=" in save("boss@x.com", other, role_ids=[owner_role])  # deactivated by a Super Admin
    assert db.load_current_user(other) is None
    assert "msg=" in save("owner@x.com", me, name="Me", is_active="1", role_ids=[owner_role],
                          new_password="a-new-password-1")


def test_team_page_locks_other_owners_for_owners(db):
    owners(db)
    page = client_for("owner@x.com").get("/admin/users").text
    assert page.count("Only a Super Admin can change this account.") == 2  # the other Owner and the Super Admin
    assert "Only a Super Admin can change this account." not in client_for("boss@x.com").get("/admin/users").text


# ── AI agent accounts ──────────────────────────────────────────────────


def test_agent_inbox_is_added_as_an_agent_and_never_an_owner(db):
    _, _, boss = owners(db)
    agent = make_user(db, "tmobile@agentmail.to", "Recruiter")
    user = db.load_current_user(agent)
    assert user.is_agent and not user.can("packages.buy") and user.can("prospects.view")

    owner_role = role_id(db, access.OWNER_ROLE)
    with pytest.raises(AccessError, match="AI agent"):
        db.update_user(agent, name="t", is_active=True, role_ids=[owner_role], actor=db.load_current_user(boss))
    with pytest.raises(AccessError, match="AI agent"):
        db.grant_owner("tmobile@agentmail.to", actor=None, role=access.SUPER_ADMIN_ROLE)
    with pytest.raises(AccessError, match="AI agent"):
        db.create_user("bot@x.com", "Bot", "long-enough-pw", [owner_role], actor=None, is_agent=True)


def test_owner_marks_and_unmarks_an_agent(db):
    owners(db)
    rep = make_user(db, "rep@x.com", "Recruiter")
    recruiter = role_id(db, "Recruiter")
    assert "msg=" in save("owner@x.com", rep, is_active="1", is_agent="1", role_ids=[recruiter])
    assert not db.load_current_user(rep).can("packages.buy")
    assert "msg=" in save("owner@x.com", rep, is_active="1", role_ids=[recruiter])
    assert db.load_current_user(rep).can("packages.buy")


def test_agent_row_can_never_act_as_owner():
    agent = access.CurrentUser(id=1, email="a@x.com", name="a",
                               roles=(access.OWNER_ROLE, access.SUPER_ADMIN_ROLE), is_agent=True)
    assert not agent.is_owner and not agent.is_super_admin and not agent.allows(access.OWNER)


# ── Autonomous use ─────────────────────────────────────────────────────


def test_autonomous_calls_are_recorded_and_ai_never_runs_owner_tools(db):
    me, _, _ = owners(db)
    owner = db.load_current_user(me)
    tools.run_tool(db, owner, "users_list", {}, source="console", surface="console")
    assert next(u for u in db.list_users() if u["id"] == me)["agent_seen_at"] is None

    result = tools.run_tool(db, owner, "users_list", {}, source="mcp:Claude", surface="console")
    assert not result["ok"] and "AI assistant" in result["error"]
    seen = next(u for u in db.list_users() if u["id"] == me)
    assert seen["agent_seen_at"] and seen["agent_seen_via"] == "mcp:Claude"
    assert "AI connector (Claude)" in client_for("owner@x.com").get("/admin/users").text

    # A CLI key is recorded too, but the remote CLI keeps its team commands.
    assert tools.run_tool(db, owner, "users_list", {}, source="cli", surface="console")["ok"]


def test_agent_inbox_stays_an_agent_when_saved_without_the_box(db):
    owners(db)
    agent = make_user(db, "tmobile@agentmail.to", "Recruiter")
    assert "msg=" in save("owner@x.com", agent, is_active="1", role_ids=[role_id(db, "Recruiter")])
    assert db.load_current_user(agent).is_agent
