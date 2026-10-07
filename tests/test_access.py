"""
Access-control tests for the web dashboard.

Run: python -m pytest tests/
"""

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import web.app as webapp  # noqa: E402
from core import access  # noqa: E402
from core.db import Database  # noqa: E402

PASSWORD = "correct-horse-battery"


@pytest.fixture
def db(pg_url, tmp_path, monkeypatch):
    monkeypatch.setattr(webapp, "DB_URL", pg_url)
    monkeypatch.setattr(webapp, "CAMPAIGNS_DIR", tmp_path / "campaigns")
    monkeypatch.setattr(webapp, "_campaigns_synced", False)
    webapp._login_failures.clear()
    webapp._reset_requests.clear()
    database = Database(pg_url)
    database.install_access()
    return database


def role_id(db: Database, name: str) -> int:
    return next(r["id"] for r in db.list_roles() if r["name"] == name)


def make_user(db: Database, email: str, *roles: str) -> int:
    return db.create_user(email, email.split("@")[0], PASSWORD,
                          [role_id(db, r) for r in roles], actor=None)


def client_for(email: str | None = None) -> TestClient:
    client = TestClient(webapp.app, follow_redirects=False)
    if email:
        r = client.post("/login", data={"email": email, "password": PASSWORD})
        assert r.status_code == 303 and webapp.SESSION_COOKIE in r.cookies, r.headers
    return client


# ── Route map ──────────────────────────────────────────────────────────


def test_every_route_has_a_rule_and_no_rule_is_stale():
    declared = {
        f"{m} {r.path}" for r in webapp.app.routes
        if hasattr(r, "methods") for m in r.methods
    }
    assert declared - set(access.ROUTE_RULES) == set()
    assert set(access.ROUTE_RULES) - declared == set()


def test_every_permission_in_rules_and_roles_is_in_catalog():
    used = {v for v in access.ROUTE_RULES.values() if not v.startswith("@")}
    for _, perms in access.STARTER_ROLES.values():
        used |= set(perms)
    assert used <= set(access.CATALOG)


def test_unmapped_route_is_denied_even_for_owner(db):
    @webapp.app.get("/__unmapped_test_route")
    async def _unmapped():
        return {"leak": True}

    # Like every real route, it must sit before the catch-all MCP mount at "/".
    routes = webapp.app.router.routes
    route = routes.pop()
    routes.insert(len(routes) - 1, route)
    try:
        make_user(db, "owner@x.com", access.OWNER_ROLE)
        assert client_for("owner@x.com").get("/__unmapped_test_route").status_code == 403
    finally:
        routes.remove(route)


# ── Authentication ─────────────────────────────────────────────────────


def test_guests_are_sent_to_login(db):
    c = client_for()
    assert c.get("/healthz").status_code == 200
    r = c.get("/prospects?q=abc")
    assert r.status_code == 303 and r.headers["location"] == "/login?next=/prospects%3Fq%3Dabc"
    assert c.get("/api/stats").status_code == 401
    assert c.get("/calendar.ics").status_code == 401
    assert c.post("/email-templates/save", data={"file_path": "x"}).status_code == 303


def test_wrong_password_and_rate_limit(db):
    make_user(db, "rep@x.com", "Sales Rep")
    c = client_for()
    for _ in range(webapp.LOGIN_MAX_FAILURES):
        r = c.post("/login", data={"email": "rep@x.com", "password": "nope"})
        assert "Incorrect" in r.headers["location"]
    # Even the right password is refused once locked out
    r = c.post("/login", data={"email": "rep@x.com", "password": PASSWORD})
    assert "Too%20many" in r.headers["location"]


def test_account_locks_after_failures_from_many_ips(db, monkeypatch):
    make_user(db, "rep@x.com", "Sales Rep")
    c = client_for()
    ips = iter(f"10.0.0.{i}" for i in range(100))
    monkeypatch.setattr(webapp, "LOGIN_MAX_FAILURES", 10**6)  # isolate the per-account limit
    for _ in range(webapp.LOGIN_ACCOUNT_MAX_FAILURES):
        r = TestClient(webapp.app, follow_redirects=False, client=(next(ips), 1)).post(
            "/login", data={"email": "rep@x.com", "password": "nope"})
        assert "Incorrect" in r.headers["location"]
    r = TestClient(webapp.app, follow_redirects=False, client=(next(ips), 1)).post(
        "/login", data={"email": "rep@x.com", "password": PASSWORD})
    assert "Too%20many" in r.headers["location"]


def test_security_headers(db):
    r = client_for().get("/login")
    assert r.headers["x-frame-options"] == "DENY"
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["referrer-policy"] == "same-origin"
    assert client_for().get("/reset-password/whatever").headers["cache-control"] == "no-store"


def test_login_redirect_rejects_offsite_next(db):
    make_user(db, "rep@x.com", "Sales Rep")
    r = client_for().post("/login", data={
        "email": "rep@x.com", "password": PASSWORD, "next": "//evil.example",
    })
    assert r.headers["location"] == "/"


def test_user_with_no_roles_lands_on_account_page(db):
    make_user(db, "new@x.com")
    c = client_for()
    r = c.post("/login", data={"email": "new@x.com", "password": PASSWORD})
    assert r.headers["location"] == "/account"
    assert c.get("/account").status_code == 200
    assert c.get("/").status_code == 403


# ── Authorization ──────────────────────────────────────────────────────


def test_caller_gets_only_caller_pages(db):
    make_user(db, "caller@x.com", "Caller")
    c = client_for("caller@x.com")
    for path in ["/", "/prospects", "/call-log", "/call-scripts", "/calendar", "/account"]:
        assert c.get(path).status_code == 200, path
    for path in ["/emails", "/email-templates", "/campaigns", "/admin/users",
                 "/admin/roles", "/admin/audit", "/prospects?print=1"]:
        assert c.get(path).status_code == 403, path
    assert c.post("/email-templates/save", data={"file_path": "x"}).status_code == 403
    assert c.post("/prospects/1/info", data={"name": "x"}).status_code == 403
    assert c.post("/admin/roles", data={"name": "Mine"}).status_code == 403


def test_nav_only_shows_permitted_pages(db):
    make_user(db, "editor@x.com", "Template Editor")
    html = client_for("editor@x.com").get("/").text
    assert 'href="/email-templates"' in html
    assert 'href="/prospects"' not in html
    assert 'href="/admin/users"' not in html


def test_multiple_roles_union(db):
    make_user(db, "both@x.com", "Caller", "Template Editor")
    c = client_for("both@x.com")
    assert c.get("/call-log").status_code == 200
    assert c.get("/email-templates").status_code == 200
    assert c.get("/emails").status_code == 403


def test_owner_has_everything(db):
    make_user(db, "owner@x.com", access.OWNER_ROLE)
    c = client_for("owner@x.com")
    for path in ["/", "/prospects", "/emails", "/email-templates", "/campaigns",
                 "/admin/users", "/admin/roles", "/admin/audit"]:
        assert c.get(path).status_code == 200, path


def test_revoking_a_role_applies_to_existing_session(db):
    make_user(db, "owner@x.com", access.OWNER_ROLE)
    rep_id = make_user(db, "rep@x.com", "Sales Rep")
    rep = client_for("rep@x.com")
    assert rep.get("/emails").status_code == 200

    owner = client_for("owner@x.com")
    r = owner.post(f"/admin/users/{rep_id}", data={
        "name": "rep", "is_active": "1", "role_ids": [role_id(db, "Caller")],
    })
    assert "msg=" in r.headers["location"]
    assert rep.get("/emails").status_code == 403
    assert rep.get("/call-log").status_code == 200


def test_deactivating_a_user_ends_their_session(db):
    make_user(db, "owner@x.com", access.OWNER_ROLE)
    rep_id = make_user(db, "rep@x.com", "Sales Rep")
    rep = client_for("rep@x.com")
    client_for("owner@x.com").post(f"/admin/users/{rep_id}", data={
        "name": "rep", "role_ids": [role_id(db, "Sales Rep")],
    })
    assert rep.get("/").status_code == 303
    r = client_for().post("/login", data={"email": "rep@x.com", "password": PASSWORD})
    assert "Incorrect" in r.headers["location"]


def test_cross_site_post_is_rejected(db):
    make_user(db, "owner@x.com", access.OWNER_ROLE)
    c = client_for("owner@x.com")
    r = c.post("/admin/roles", data={"name": "Evil"},
               headers={"origin": "https://evil.example"})
    assert r.status_code == 403
    assert all(role["name"] != "Evil" for role in db.list_roles())


# ── Owner invariants ───────────────────────────────────────────────────


def test_last_owner_cannot_be_demoted_or_deactivated(db):
    owner_id = make_user(db, "owner@x.com", access.OWNER_ROLE)
    c = client_for("owner@x.com")
    r = c.post(f"/admin/users/{owner_id}", data={"name": "owner", "is_active": "1", "role_ids": []})
    assert "error=" in r.headers["location"]
    r = c.post(f"/admin/users/{owner_id}", data={
        "name": "owner", "role_ids": [role_id(db, access.OWNER_ROLE)],
    })
    assert "error=" in r.headers["location"]
    assert db.load_current_user(owner_id).is_owner


def test_second_owner_allows_demoting_first(db):
    first = make_user(db, "first@x.com", access.OWNER_ROLE)
    make_user(db, "second@x.com", access.SUPER_ADMIN_ROLE)  # only a Super Admin may remove Owner
    r = client_for("second@x.com").post(f"/admin/users/{first}", data={
        "name": "first", "is_active": "1", "role_ids": [role_id(db, "Viewer")],
    })
    assert "msg=" in r.headers["location"]
    assert not db.load_current_user(first).is_owner


def test_owner_role_is_protected(db):
    make_user(db, "owner@x.com", access.OWNER_ROLE)
    c = client_for("owner@x.com")
    owner_role = role_id(db, access.OWNER_ROLE)
    r = c.post(f"/admin/roles/{owner_role}", data={"name": "Owner", "permissions": []})
    assert "error=" in r.headers["location"]
    r = c.post(f"/admin/roles/{owner_role}/delete")
    assert "error=" in r.headers["location"]


def test_roles_only_accept_catalog_permissions(db):
    make_user(db, "owner@x.com", access.OWNER_ROLE)
    c = client_for("owner@x.com")
    r = c.post("/admin/roles", data={"name": "Sneaky", "permissions": ["everything"]})
    assert "error=" in r.headers["location"]
    r = c.post("/admin/roles", data={"name": "Reader", "permissions": ["emails.view"]})
    assert "msg=" in r.headers["location"]
    audit = db.list_audit()
    assert audit[0]["action"] == "role.create" and audit[0]["actor_label"] == "owner@x.com"


def test_installer_preserves_edited_starter_roles(db):
    caller = role_id(db, "Caller")
    db.update_role(caller, "Caller", "edited", ["calls.view"], actor=None)
    db.install_access()
    role = next(r for r in db.list_roles() if r["id"] == caller)
    assert role["permissions"] == {"calls.view"}


# ── Attribution ────────────────────────────────────────────────────────


def test_calls_are_attributed_to_signed_in_user(db):
    make_user(db, "caller@x.com", "Caller")
    c = db.conn
    prospect_id = c.execute("INSERT INTO prospects (name) VALUES ('Org') RETURNING id").fetchone()[0]
    campaign_id = c.execute("INSERT INTO campaigns (name) VALUES ('camp') RETURNING id").fetchone()[0]
    outreach_id = c.execute(
        "INSERT INTO outreach (prospect_id, campaign_id) VALUES (?, ?) RETURNING id",
        (prospect_id, campaign_id),
    ).fetchone()[0]

    r = client_for("caller@x.com").post("/call-log/record", data={
        "prospect_id": prospect_id, "outreach_id": outreach_id, "campaign_id": campaign_id,
        "outcome": "voicemail", "called_by": "Somebody Else",
    })
    assert r.status_code == 303
    row = c.execute("SELECT called_by FROM call_log").fetchone()
    assert row["called_by"] == "caller"


# ── Welcome emails ─────────────────────────────────────────────────────


def invite(db: Database, user_id: int) -> str:
    from core import welcome

    link, _ = welcome.issue_invite(db, user_id, "https://dash.example")
    return link.removeprefix("https://dash.example")


def test_welcome_link_sets_password_and_signs_in(db):
    rep_id = make_user(db, "rep@x.com", "Sales Rep")
    path = invite(db, rep_id)
    c = client_for()
    assert "rep@x.com" in c.get(path).text
    r = c.post(path, data={"new_password": "brand-new-pass", "confirm_password": "brand-new-pass"})
    assert r.status_code == 303 and r.headers["location"] == "/"
    assert c.get("/emails").status_code == 200
    # Link is single-use; the new password works
    assert c.get(path).status_code == 410
    r = client_for().post("/login", data={"email": "rep@x.com", "password": "brand-new-pass"})
    assert r.status_code == 303 and webapp.SESSION_COOKIE in r.cookies
    assert "auth.invite_accepted" in [a["action"] for a in db.list_audit()]


def test_welcome_link_rejects_mismatch_short_and_superseded(db):
    rep_id = make_user(db, "rep@x.com", "Caller")
    old = invite(db, rep_id)
    new = invite(db, rep_id)
    c = client_for()
    assert c.get(old).status_code == 410
    r = c.post(new, data={"new_password": "brand-new-pass", "confirm_password": "different-pass"})
    assert "error=" in r.headers["location"]
    r = c.post(new, data={"new_password": "short", "confirm_password": "short"})
    assert "error=" in r.headers["location"]
    assert c.get(new).status_code == 200


def test_welcome_link_dead_for_deactivated_or_expired_user(db):
    make_user(db, "owner@x.com", access.OWNER_ROLE)
    rep_id = make_user(db, "rep@x.com", "Caller")
    path = invite(db, rep_id)
    db.conn.execute("UPDATE invites SET expires_at = '2000-01-01'")
    assert client_for().get(path).status_code == 410

    path = invite(db, rep_id)
    db.update_user(rep_id, name="rep", is_active=False, role_ids=[], actor=None)
    r = client_for().post(path, data={"new_password": "brand-new-pass", "confirm_password": "brand-new-pass"})
    assert "error=" in r.headers["location"]


# ── Password reset ─────────────────────────────────────────────────────


@pytest.fixture
def outbox(monkeypatch):
    """Pretend SMTP is configured and capture what would be sent."""
    from core.models import SendResult

    sent: list[tuple[str, str, str]] = []
    monkeypatch.setattr(webapp.welcome_email, "smtp_configured", lambda: True)
    monkeypatch.setattr(webapp.welcome_email, "send",
                        lambda to, subject, body: sent.append((to, subject, body)) or SendResult(status="sent"))
    return sent


def reset_path(body: str) -> str:
    link = next(w for w in body.split() if "/reset-password/" in w)
    return link[link.index("/reset-password/"):]


def test_forgot_password_emails_link_that_resets_and_signs_in(db, outbox):
    make_user(db, "rep@x.com", "Sales Rep")
    c = client_for()
    assert 'href="/forgot-password"' in c.get("/login").text
    r = c.post("/forgot-password", data={"email": " Rep@X.com "})
    assert r.headers["location"] == "/forgot-password?sent=1"
    assert len(outbox) == 1 and outbox[0][0] == "rep@x.com"
    path = reset_path(outbox[0][2])

    old_session = client_for("rep@x.com")
    assert "Reset your password" in c.get(path).text
    r = c.post(path, data={"new_password": "brand-new-pass", "confirm_password": "brand-new-pass"})
    assert r.status_code == 303 and r.headers["location"] == "/"
    assert c.get(path).status_code == 410
    # Other sessions are signed out; the new password works, the old one doesn't
    assert old_session.get("/account").status_code == 303
    assert client_for().post("/login", data={"email": "rep@x.com", "password": PASSWORD}) \
        .headers["location"].startswith("/login?error=")
    r = client_for().post("/login", data={"email": "rep@x.com", "password": "brand-new-pass"})
    assert webapp.SESSION_COOKIE in r.cookies
    actions = [a["action"] for a in db.list_audit()]
    assert "user.password_reset_requested" in actions and "auth.password_reset" in actions


def test_forgot_password_same_answer_for_unknown_and_inactive(db, outbox):
    make_user(db, "owner@x.com", access.OWNER_ROLE)
    rep_id = make_user(db, "rep@x.com", "Caller")
    db.update_user(rep_id, name="rep", is_active=False, role_ids=[], actor=None)
    c = client_for()
    for email in ("nobody@x.com", "rep@x.com"):
        assert c.post("/forgot-password", data={"email": email}).headers["location"] == "/forgot-password?sent=1"
    assert outbox == []


def test_forgot_password_rate_limited_and_link_expires(db, outbox):
    make_user(db, "rep@x.com", "Caller")
    c = client_for()
    for _ in range(webapp.RESET_MAX_REQUESTS):
        c.post("/forgot-password", data={"email": "rep@x.com"})
    r = c.post("/forgot-password", data={"email": "rep@x.com"})
    assert "error=" in r.headers["location"]
    assert len(outbox) == webapp.RESET_MAX_REQUESTS
    # Only the newest link works, and only until it expires
    assert c.get(reset_path(outbox[0][2])).status_code == 410
    newest = reset_path(outbox[-1][2])
    assert c.get(newest).status_code == 200
    db.conn.execute("UPDATE invites SET expires_at = '2000-01-01'")
    assert c.get(newest).status_code == 410


def test_forgot_password_without_smtp_points_to_an_owner(db):
    make_user(db, "rep@x.com", "Caller")
    c = client_for()
    assert "Ask an owner" in c.get("/forgot-password").text
    c.post("/forgot-password", data={"email": "rep@x.com"})
    assert db.conn.execute("SELECT COUNT(*) AS n FROM invites").fetchone()["n"] == 0


def test_cli_invite_creates_user_and_prints_email(db):
    from click.testing import CliRunner
    from core.cli import cli

    r = CliRunner().invoke(cli, [
        "--db", webapp.DB_URL, "users", "invite", "--email", "New.Rep@x.com",
        "--name", "Jane Rep", "--role", "sales rep", "--base-url", "https://dash.example/",
        "--no-send",
    ])
    assert r.exit_code == 0, r.output
    assert "Hi Jane," in r.output and "Edit organization and contact info" in r.output
    link = next(w for w in r.output.split() if w.startswith("https://dash.example/welcome/"))
    user = db.load_current_user(db.get_user_by_email("new.rep@x.com")["id"])
    assert user.roles == ("Sales Rep",)
    assert client_for().get(link.removeprefix("https://dash.example")).status_code == 200

    r = CliRunner().invoke(cli, ["--db", webapp.DB_URL, "users", "invite",
                                 "--email", "x@x.com", "--role", "Nope", "--base-url", "https://d", "--no-send"])
    assert r.exit_code == 1 and "Unknown role" in r.output
