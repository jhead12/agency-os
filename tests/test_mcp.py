"""
The MCP server (/mcp) with personal tokens and OAuth 2.1 (Part B phases 3-4).

Runs the real app (with its lifespan, so the MCP session manager is up)
against the test database. Nothing leaves the process.

Run: TEST_DATABASE_URL=postgresql://localhost/agency_os_test python -m pytest tests/test_mcp.py
"""

import base64
import hashlib
import json
import secrets
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import web.app as webapp  # noqa: E402
from core import mcp_auth  # noqa: E402
from core.models import Prospect  # noqa: E402
from tests.test_access import PASSWORD, db, make_user  # noqa: E402,F401

BASE = "http://127.0.0.1:8000"
ACCEPT = "application/json, text/event-stream"


@pytest.fixture
def app(db, monkeypatch):
    monkeypatch.setenv("AGENCY_OS_BASE_URL", BASE)
    for var in ("AGENCY_OS_AI", "AGENCY_OS_RUN_JOBS", "AGENCY_OS_OWNER_EMAIL", "AGENCY_OS_OWNER_PASSWORD"):
        monkeypatch.delenv(var, raising=False)
    with TestClient(webapp.app, base_url=BASE, follow_redirects=False) as client:
        yield client


def rep(db, email="rep@x.com", ai=True):
    user_id = make_user(db, email, "Sales Rep")
    if ai:
        db.set_ai_enabled(user_id, True, actor=None)
    return db.load_current_user(user_id)


def prospect(db):
    campaign_id = db.upsert_campaign("mcp-test", "x")
    prospect_id = db.upsert_prospect(Prospect(name="Civic Org", state="CA", city="Los Angeles"))
    db.upsert_outreach(prospect_id, campaign_id)
    return prospect_id


def rpc(client, token, method, params=None):
    headers = {"Accept": ACCEPT, "MCP-Protocol-Version": "2025-06-18"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    response = client.post("/mcp", headers=headers, json={"jsonrpc": "2.0", "id": 1, "method": method,
                                                           "params": params or {}})
    return response


def result(client, token, method, params=None):
    response = rpc(client, token, method, params)
    assert response.status_code == 200, response.text
    body = response.json()
    assert "error" not in body, body
    return body["result"]


def call(client, token, name, args):
    out = result(client, token, "tools/call", {"name": name, "arguments": args})
    return out, json.loads(out["content"][0]["text"])


# ── Personal tokens ────────────────────────────────────────────────────


def test_no_or_bad_token_is_refused_with_oauth_pointer(app, db):
    missing = rpc(app, None, "tools/list")
    assert missing.status_code == 401
    assert "oauth-protected-resource/mcp" in missing.headers["www-authenticate"]
    assert rpc(app, "aos_pat_not-a-real-token", "tools/list").status_code == 401
    meta = app.get("/.well-known/oauth-protected-resource/mcp").json()
    assert meta["resource"] == f"{BASE}/mcp" and meta["authorization_servers"] == [BASE]


def test_read_only_token(app, db):
    pid = prospect(db)
    user = rep(db)
    token = mcp_auth.create_personal_token(db, user, "Hermes", allow_writes=False)
    names = {t["name"] for t in result(app, token, "tools/list")["tools"]}
    assert "get_prospect" in names and "set_stage" not in names and "add_prospect_note" not in names

    out, data = call(app, token, "get_prospect", {"prospect_id": pid})
    assert not out["isError"] and data["prospect"]["name"] == "Civic Org"
    out, data = call(app, token, "set_stage", {"prospect_id": pid, "stage": "engaged"})
    assert out["isError"] and "can't make changes" in data["error"]


def test_write_token_changes_and_audits(app, db):
    pid = prospect(db)
    token = mcp_auth.create_personal_token(db, rep(db), "Claude Desktop", allow_writes=True)
    out, data = call(app, token, "set_stage", {"prospect_id": pid, "stage": "engaged"})
    assert not out["isError"] and data["to"] == "engaged"
    audit = db.conn.execute("SELECT details FROM audit_log WHERE action = 'tool.set_stage'").fetchone()
    assert json.loads(audit["details"])["source"] == "mcp:Claude Desktop"
    out, data = call(app, token, "set_stage", {"prospect_id": pid, "stage": "won!"})
    assert out["isError"] and "must be one of" in data["error"]


def test_token_stops_when_ai_is_off_or_revoked(app, db, monkeypatch):
    user = rep(db)
    token = mcp_auth.create_personal_token(db, user, "Rook", allow_writes=False)
    assert result(app, token, "tools/list")["tools"]
    db.set_ai_enabled(user.id, False, actor=None)
    assert result(app, token, "tools/list")["tools"] == []
    out, data = call(app, token, "get_prospect", {"prospect_id": 1})
    assert out["isError"] and "AI features are off" in data["error"]
    db.set_ai_enabled(user.id, True, actor=None)
    monkeypatch.setenv("AGENCY_OS_AI", "off")
    assert result(app, token, "tools/list")["tools"] == []
    monkeypatch.delenv("AGENCY_OS_AI")

    [row] = mcp_auth.list_tokens(db, user.id)
    assert row["label"] == "Rook" and not row["allow_writes"]
    assert mcp_auth.revoke(db, user, row["id"])
    assert rpc(app, token, "tools/list").status_code == 401
    assert not mcp_auth.revoke(db, rep(db, "other@x.com"), row["id"])


def test_prompts_and_resources(app, db):
    pid = prospect(db)
    token = mcp_auth.create_personal_token(db, rep(db), "Hermes", allow_writes=False)
    prompts = {p["name"] for p in result(app, token, "prompts/list")["prompts"]}
    assert "sales-outbound-strategist.next_email" in prompts and "sales-deal-strategist.meddpicc" in prompts
    got = result(app, token, "prompts/get", {"name": "sales-outbound-strategist.next_email",
                                             "arguments": {"prospect_id": str(pid), "instructions": "Be brief"}})
    text = got["messages"][0]["content"]["text"]
    assert "Outbound Strategist" in text and "Civic Org" in text and "Be brief" in text

    personas = result(app, token, "resources/list")["resources"]
    assert any(r["uri"] == "agency://agents/sales-engineer" for r in personas)
    read = result(app, token, "resources/read", {"uri": f"agency://prospects/{pid}"})
    assert json.loads(read["contents"][0]["text"])["name"] == "Civic Org"
    templates = result(app, token, "resources/templates/list")["resourceTemplates"]
    assert templates[0]["uriTemplate"] == "agency://prospects/{prospect_id}"


def test_account_page_creates_shows_once_and_revokes(app, db):
    user = rep(db)
    app.post("/login", data={"email": "rep@x.com", "password": PASSWORD})
    page = app.get("/account").text
    assert "Connect an AI app (MCP)" in page and f"{BASE}/mcp" in page
    created = app.post("/account/tokens", data={"name": "Hermes laptop", "allow_writes": "1"})
    assert created.status_code == 200 and "shown only this once" in created.text
    token = created.text.split('id="new-token">')[1].split("<")[0]
    assert token.startswith("aos_pat_") and "~/.hermes/config.yaml" in created.text
    assert token not in app.get("/account").text  # never shown again
    assert result(app, token, "tools/list")["tools"]
    [row] = mcp_auth.list_tokens(db, user.id)
    assert row["allow_writes"] and "Hermes laptop" in app.get("/account").text
    app.post(f"/account/tokens/{row['id']}/revoke")
    assert rpc(app, token, "tools/list").status_code == 401


# ── OAuth 2.1 (Claude.ai / ChatGPT connectors) ─────────────────────────


def pkce():
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def register(app):
    response = app.post("/register", json={
        "client_name": "Claude", "redirect_uris": ["https://claude.ai/api/mcp/auth_callback"],
        "grant_types": ["authorization_code", "refresh_token"], "response_types": ["code"],
        "token_endpoint_auth_method": "none", "scope": "agency:read agency:write",
    })
    assert response.status_code == 201, response.text
    return response.json()["client_id"]


def authorize(app, client_id, challenge, state="xyz"):
    response = app.get("/authorize", params={
        "response_type": "code", "client_id": client_id, "redirect_uri": "https://claude.ai/api/mcp/auth_callback",
        "code_challenge": challenge, "code_challenge_method": "S256", "state": state, "scope": "agency:read",
    })
    assert response.status_code in (302, 307), response.text
    location = response.headers["location"]
    assert location.startswith(f"{BASE}/oauth/consent?request=")
    return parse_qs(urlsplit(location).query)["request"][0]


def exchange(app, client_id, code, verifier):
    return app.post("/token", data={
        "grant_type": "authorization_code", "code": code, "client_id": client_id, "code_verifier": verifier,
        "redirect_uri": "https://claude.ai/api/mcp/auth_callback",
    })


def test_oauth_connector_flow(app, db):
    pid = prospect(db)
    rep(db)
    assert app.get("/.well-known/oauth-authorization-server").json()["issuer"].rstrip("/") == BASE
    client_id = register(app)
    verifier, challenge = pkce()
    request_id = authorize(app, client_id, challenge)

    # The consent page sits behind the normal agency-os login.
    assert app.get(f"/oauth/consent?request={request_id}").status_code == 303
    app.post("/login", data={"email": "rep@x.com", "password": PASSWORD})
    consent = app.get(f"/oauth/consent?request={request_id}").text
    assert "Claude wants to use agency-os as you" in consent and "claude.ai" in consent

    approved = app.post("/oauth/consent", data={"request": request_id, "decision": "approve", "allow_writes": "1"})
    back = urlsplit(approved.headers["location"])
    assert back.netloc == "claude.ai"
    query = parse_qs(back.query)
    assert query["state"] == ["xyz"]
    code = query["code"][0]

    assert exchange(app, client_id, code, "wrong-verifier").status_code == 400  # PKCE
    tokens = exchange(app, client_id, code, verifier).json()
    assert tokens["access_token"].startswith("aos_at_") and set(tokens["scope"].split()) == {"agency:read", "agency:write"}
    assert exchange(app, client_id, code, verifier).status_code == 400  # a code works once

    access = tokens["access_token"]
    out, data = call(app, access, "set_stage", {"prospect_id": pid, "stage": "engaged"})
    assert not out["isError"]
    assert "Claude" in app.get("/account").text  # listed as a connected app

    # Refresh rotates: the new pair works, the old refresh token and access token don't.
    refreshed = app.post("/token", data={"grant_type": "refresh_token", "refresh_token": tokens["refresh_token"],
                                         "client_id": client_id}).json()
    assert refreshed["access_token"] != access
    assert app.post("/token", data={"grant_type": "refresh_token", "refresh_token": tokens["refresh_token"],
                                    "client_id": client_id}).status_code == 400
    assert rpc(app, access, "tools/list").status_code == 401
    assert result(app, refreshed["access_token"], "tools/list")["tools"]

    # MCP SDK 2.3's /revoke requires a client_secret field even for public clients; empty is accepted.
    revoked = app.post("/revoke", data={"token": refreshed["refresh_token"], "client_id": client_id, "client_secret": ""})
    assert revoked.status_code == 200, revoked.text
    assert rpc(app, refreshed["access_token"], "tools/list").status_code == 401


def test_oauth_deny_expiry_and_ai_off(app, db):
    user = rep(db)
    client_id = register(app)
    _verifier, challenge = pkce()
    app.post("/login", data={"email": "rep@x.com", "password": PASSWORD})

    request_id = authorize(app, client_id, challenge)
    denied = app.post("/oauth/consent", data={"request": request_id, "decision": "deny"})
    assert parse_qs(urlsplit(denied.headers["location"]).query)["error"] == ["access_denied"]
    reused = app.post("/oauth/consent", data={"request": request_id, "decision": "approve"})
    assert "expired" in reused.headers["location"]  # a consent request is single-use
    assert "expired or was already used" in app.get("/oauth/consent?request=nope").text

    db.set_ai_enabled(user.id, False, actor=None)
    request_id = authorize(app, client_id, challenge)
    assert "AI features are off" in app.get(f"/oauth/consent?request={request_id}").text
    blocked = app.post("/oauth/consent", data={"request": request_id, "decision": "approve"})
    assert blocked.headers["location"].startswith("/account?error=")


def test_read_only_oauth_grant(app, db):
    rep(db)
    client_id = register(app)
    verifier, challenge = pkce()
    request_id = authorize(app, client_id, challenge)
    app.post("/login", data={"email": "rep@x.com", "password": PASSWORD})
    approved = app.post("/oauth/consent", data={"request": request_id, "decision": "approve"})
    code = parse_qs(urlsplit(approved.headers["location"]).query)["code"][0]
    tokens = exchange(app, client_id, code, verifier).json()
    assert tokens["scope"] == "agency:read"
    names = {t["name"] for t in result(app, tokens["access_token"], "tools/list")["tools"]}
    assert "set_stage" not in names and "get_prospect" in names
