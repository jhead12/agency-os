"""
Who an MCP client is: personal access tokens and OAuth 2.1.

- Personal tokens (`aos_pat_...`) are made on the Account page for local
  agents (Hermes Agent, Claude Desktop/Code, Rook...) and sent as
  `Authorization: Bearer`. They're read-only unless "allow changes" was ticked.
- OAuth (authorization code + PKCE, dynamic client registration, rotating
  refresh tokens) lets hosted assistants (Claude.ai, ChatGPT connectors) sign
  in. The MCP SDK serves the endpoints; AgencyOAuthProvider stores everything
  here, and the consent screen is an agency-os page behind the normal login.

Only SHA-256 hashes of tokens, codes and consent requests are stored. Every
token acts as the user who made it, with that user's permissions, and stops
working when they turn AI features off, are deactivated, or revoke it.
"""

from __future__ import annotations

import json
import secrets
from typing import Callable, Optional

import anyio
from mcp.server.auth.provider import (
    AccessToken, AuthorizationCode, AuthorizationParams, AuthorizeError, RefreshToken, TokenError,
    construct_redirect_uri,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

from core.access import hash_token

READ = "agency:read"
WRITE = "agency:write"
SCOPES = [READ, WRITE]
PERSONAL_CLIENT = "personal"
ACCESS_TTL = 3600                 # one hour
REFRESH_TTL = 30 * 24 * 3600      # thirty days
CODE_TTL = 300
REQUEST_TTL = 600
MAX_PERSONAL_TOKENS = 20


def _new(prefix: str) -> str:
    return f"{prefix}{secrets.token_urlsafe(32)}"


def _scopes(text: str) -> list[str]:
    return [s for s in (text or "").split() if s in SCOPES]


# ── Personal tokens (Account page) ─────────────────────────────────────


def create_personal_token(db, user, name: str, allow_writes: bool) -> str:
    """Make a personal token and return it; it's never retrievable again."""
    name = (name or "").strip()[:80] or "MCP client"
    c = db.conn
    count = c.execute("""SELECT COUNT(*) AS n FROM api_tokens
                         WHERE user_id = ? AND kind = 'personal' AND revoked_at IS NULL""", (user.id,)).fetchone()["n"]
    if count >= MAX_PERSONAL_TOKENS:
        raise ValueError(f"You already have {MAX_PERSONAL_TOKENS} access keys; disconnect one first.")
    token = _new("aos_pat_")
    scopes = " ".join([READ, WRITE] if allow_writes else [READ])
    with c.raw.transaction():
        token_id = c.execute(
            """INSERT INTO api_tokens (user_id, kind, name, token_hash, client_id, scopes)
               VALUES (?, 'personal', ?, ?, ?, ?) RETURNING id""",
            (user.id, name, hash_token(token), PERSONAL_CLIENT, scopes),
        ).fetchone()["id"]
        db._audit(c, user, "token.create", "api_token", token_id, {"name": name, "allow_writes": allow_writes})
    return token


def list_tokens(db, user_id: int) -> list[dict]:
    """A user's live personal tokens and OAuth connections (never the secrets)."""
    rows = db.conn.execute(
        """SELECT t.id, t.kind, t.name, t.client_id, t.scopes, t.created_at, t.last_used_at, c.info
           FROM api_tokens t LEFT JOIN oauth_clients c ON c.client_id = t.client_id
           WHERE t.user_id = ? AND t.revoked_at IS NULL AND t.kind IN ('personal', 'refresh')
             AND (t.expires_at IS NULL OR t.expires_at > CURRENT_TIMESTAMP)
           ORDER BY t.created_at DESC""", (user_id,)).fetchall()
    out = []
    for r in rows:
        row = dict(r)
        info = json.loads(row.pop("info") or "{}")
        row["label"] = row["name"] if row["kind"] == "personal" else (info.get("client_name") or "Connected app")
        row["allow_writes"] = WRITE in row["scopes"].split()
        out.append(row)
    return out


def revoke(db, user, token_id: int) -> bool:
    """Revoke one of the user's tokens (and its OAuth family). False if it isn't theirs."""
    c = db.conn
    with c.raw.transaction():
        row = c.execute("SELECT id, family FROM api_tokens WHERE id = ? AND user_id = ? AND revoked_at IS NULL",
                        (token_id, user.id)).fetchone()
        if row is None:
            return False
        c.execute("""UPDATE api_tokens SET revoked_at = CURRENT_TIMESTAMP
                     WHERE revoked_at IS NULL AND (id = ? OR (family IS NOT NULL AND family = ?))""",
                  (row["id"], row["family"]))
        db._audit(c, user, "token.revoke", "api_token", token_id, {})
    return True


# ── Consent requests (the /oauth/consent page) ─────────────────────────


def load_request(db, request_id: str) -> Optional[tuple[OAuthClientInformationFull, AuthorizationParams]]:
    row = db.conn.execute(
        """SELECT r.params, c.info FROM oauth_requests r JOIN oauth_clients c ON c.client_id = r.client_id
           WHERE r.request_hash = ? AND r.expires_at > CURRENT_TIMESTAMP""", (hash_token(request_id),)).fetchone()
    if row is None:
        return None
    return OAuthClientInformationFull.model_validate_json(row["info"]), AuthorizationParams.model_validate_json(row["params"])


def decide(db, user, request_id: str, approve: bool, allow_writes: bool) -> Optional[str]:
    """Finish a consent request; returns where to send the browser (None if it expired or was used)."""
    c = db.conn
    with c.raw.transaction():
        row = c.execute("""DELETE FROM oauth_requests WHERE request_hash = ? AND expires_at > CURRENT_TIMESTAMP
                           RETURNING client_id, params""", (hash_token(request_id),)).fetchone()
        if row is None:
            return None
        params = AuthorizationParams.model_validate_json(row["params"])
        if not approve:
            db._audit(c, user, "oauth.deny", "oauth_client", row["client_id"], {})
            return construct_redirect_uri(str(params.redirect_uri), error="access_denied",
                                          error_description="The user declined", state=params.state)
        # Read is always granted; changes only when the user ticks the box on the
        # consent page (whether or not the app asked; most only ask for read).
        scopes = [READ, WRITE] if allow_writes else [READ]
        code = secrets.token_urlsafe(32)
        c.execute(
            """INSERT INTO oauth_codes (code_hash, client_id, user_id, scopes, code_challenge, redirect_uri,
                   redirect_uri_explicit, resource, expires_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP + make_interval(secs => ?))""",
            (hash_token(code), row["client_id"], user.id, " ".join(scopes), params.code_challenge,
             str(params.redirect_uri), int(params.redirect_uri_provided_explicitly), params.resource, CODE_TTL),
        )
        db._audit(c, user, "oauth.approve", "oauth_client", row["client_id"], {"scopes": scopes})
    return construct_redirect_uri(str(params.redirect_uri), code=code, state=params.state)


# ── The provider the MCP SDK calls ─────────────────────────────────────


class AgencyOAuthProvider:
    """OAuthAuthorizationServerProvider backed by the agency-os database.

    The SDK validates PKCE, redirect URIs and expiry around these calls; this
    class stores state, issues tokens, and makes codes and refresh tokens single-use.
    """

    def __init__(self, get_db: Callable, consent_url: str):
        self.get_db = get_db
        self.consent_url = consent_url

    async def _run(self, fn, *args):
        return await anyio.to_thread.run_sync(fn, *args)

    # Clients (dynamic registration)

    async def get_client(self, client_id: str) -> Optional[OAuthClientInformationFull]:
        def load():
            row = self.get_db().conn.execute("SELECT info FROM oauth_clients WHERE client_id = ?",
                                             (client_id,)).fetchone()
            return OAuthClientInformationFull.model_validate_json(row["info"]) if row else None
        return await self._run(load)

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        def save():
            self.get_db().conn.execute(
                "INSERT INTO oauth_clients (client_id, info) VALUES (?, ?) ON CONFLICT (client_id) DO NOTHING",
                (client_info.client_id, client_info.model_dump_json()))
        await self._run(save)

    # Authorization: hand the browser to the consent page

    async def authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        if params.scopes and any(s not in SCOPES for s in params.scopes):
            raise AuthorizeError("invalid_scope", "Unknown scope requested")
        request_id = secrets.token_urlsafe(24)

        def save():
            c = self.get_db().conn
            c.execute("DELETE FROM oauth_requests WHERE expires_at < CURRENT_TIMESTAMP")
            c.execute("""INSERT INTO oauth_requests (request_hash, client_id, params, expires_at)
                         VALUES (?, ?, ?, CURRENT_TIMESTAMP + make_interval(secs => ?))""",
                      (hash_token(request_id), client.client_id, params.model_dump_json(), REQUEST_TTL))
        await self._run(save)
        return construct_redirect_uri(self.consent_url, request=request_id)

    async def load_authorization_code(self, client, authorization_code: str) -> Optional[AuthorizationCode]:
        def load():
            row = self.get_db().conn.execute(
                """SELECT *, EXTRACT(EPOCH FROM expires_at)::float AS exp FROM oauth_codes
                   WHERE code_hash = ? AND client_id = ? AND used_at IS NULL""",
                (hash_token(authorization_code), client.client_id)).fetchone()
            if row is None:
                return None
            return AuthorizationCode(
                code=authorization_code, scopes=_scopes(row["scopes"]), expires_at=row["exp"],
                client_id=row["client_id"], code_challenge=row["code_challenge"], redirect_uri=row["redirect_uri"],
                redirect_uri_provided_explicitly=bool(row["redirect_uri_explicit"]), resource=row["resource"],
                subject=str(row["user_id"]))
        return await self._run(load)

    def _issue(self, c, user_id: int, client_id: str, scopes: list[str], resource: Optional[str]) -> OAuthToken:
        access, refresh, family = _new("aos_at_"), _new("aos_rt_"), secrets.token_hex(16)
        for kind, token, ttl in (("access", access, ACCESS_TTL), ("refresh", refresh, REFRESH_TTL)):
            c.execute(
                """INSERT INTO api_tokens (user_id, kind, token_hash, client_id, scopes, resource, family, expires_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP + make_interval(secs => ?))""",
                (user_id, kind, hash_token(token), client_id, " ".join(scopes), resource, family, ttl))
        return OAuthToken(access_token=access, expires_in=ACCESS_TTL, scope=" ".join(scopes), refresh_token=refresh)

    async def exchange_authorization_code(self, client, authorization_code: AuthorizationCode) -> OAuthToken:
        def exchange():
            c = self.get_db().conn
            with c.raw.transaction():
                row = c.execute("""UPDATE oauth_codes SET used_at = CURRENT_TIMESTAMP
                                   WHERE code_hash = ? AND client_id = ? AND used_at IS NULL RETURNING user_id""",
                                (hash_token(authorization_code.code), client.client_id)).fetchone()
                if row is None:
                    raise TokenError("invalid_grant", "Authorization code already used")
                return self._issue(c, row["user_id"], client.client_id, authorization_code.scopes,
                                   authorization_code.resource)
        return await self._run(exchange)

    async def load_refresh_token(self, client, refresh_token: str) -> Optional[RefreshToken]:
        def load():
            row = self._live(refresh_token, ("refresh",))
            if row is None or row["client_id"] != client.client_id:
                return None
            return RefreshToken(token=refresh_token, client_id=row["client_id"], scopes=_scopes(row["scopes"]),
                                expires_at=row["exp"], resource=row["resource"], subject=str(row["user_id"]))
        return await self._run(load)

    async def exchange_refresh_token(self, client, refresh_token: RefreshToken, scopes: list[str]) -> OAuthToken:
        def exchange():
            c = self.get_db().conn
            with c.raw.transaction():
                # Rotate: the old refresh token (and its access token) die now; reuse fails.
                row = c.execute("""UPDATE api_tokens SET revoked_at = CURRENT_TIMESTAMP
                                   WHERE token_hash = ? AND kind = 'refresh' AND revoked_at IS NULL
                                   RETURNING user_id, family, scopes""",
                                (hash_token(refresh_token.token),)).fetchone()
                if row is None:
                    raise TokenError("invalid_grant", "Refresh token already used or revoked")
                c.execute("UPDATE api_tokens SET revoked_at = CURRENT_TIMESTAMP WHERE family = ? AND revoked_at IS NULL",
                          (row["family"],))
                granted = _scopes(row["scopes"])
                wanted = [s for s in (scopes or granted) if s in granted] or granted
                return self._issue(c, row["user_id"], client.client_id, wanted, refresh_token.resource)
        return await self._run(exchange)

    # Verifying bearer tokens (personal and OAuth access tokens)

    def _live(self, token: str, kinds: tuple[str, ...]) -> Optional[dict]:
        if not token.startswith("aos_"):
            return None
        row = self.get_db().conn.execute(
            """SELECT *, EXTRACT(EPOCH FROM expires_at)::bigint AS exp,
                      (last_used_at IS NULL OR last_used_at < CURRENT_TIMESTAMP - INTERVAL '1 minute') AS stale
               FROM api_tokens WHERE token_hash = ? AND kind = ANY(?) AND revoked_at IS NULL
                 AND (expires_at IS NULL OR expires_at > CURRENT_TIMESTAMP)""",
            (hash_token(token), list(kinds))).fetchone()
        return dict(row) if row else None

    async def load_access_token(self, token: str) -> Optional[AccessToken]:
        def load():
            row = self._live(token, ("personal", "access"))
            if row is None:
                return None
            if row["stale"]:  # record use at most once a minute, not on every request
                self.get_db().conn.execute("UPDATE api_tokens SET last_used_at = CURRENT_TIMESTAMP WHERE id = ?",
                                           (row["id"],))
            return AccessToken(token=token, client_id=row["client_id"], scopes=_scopes(row["scopes"]),
                               expires_at=row["exp"], resource=row["resource"], subject=str(row["user_id"]),
                               claims={"token_name": row["name"] or row["client_id"]})
        return await self._run(load)

    async def revoke_token(self, token) -> None:
        def revoke_family():
            c = self.get_db().conn
            row = c.execute("SELECT id, family FROM api_tokens WHERE token_hash = ?", (hash_token(token.token),)).fetchone()
            if row:
                c.execute("""UPDATE api_tokens SET revoked_at = CURRENT_TIMESTAMP WHERE revoked_at IS NULL
                             AND (id = ? OR (family IS NOT NULL AND family = ?))""", (row["id"], row["family"]))
        await self._run(revoke_family)

