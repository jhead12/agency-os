"""
Users, roles, and permissions for agency-os.

Modeled on the u9itus.dev staff permission system:

- Permissions are a fixed, code-defined catalog. Owners create named roles
  by picking from it; a role name alone never grants anything.
- The protected Owner role bypasses permission checks and is the only role
  that can manage users, roles, and the audit log.
- Every web route must appear in ROUTE_RULES. Unlisted routes are denied.
- New users get no access until an owner assigns them a role.

This module holds policy only (no SQL). Persistence lives in core/db.py.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass, field

OWNER_ROLE = "Owner"


class AccessError(ValueError):
    """A user/role change was rejected (invalid input or a broken invariant)."""

# ── Permission catalog ─────────────────────────────────────────────────

CATALOG: dict[str, str] = {
    "dashboard.view": "View the dashboard and pipeline stats",
    "prospects.view": "View the prospect list and prospect detail pages",
    "prospects.export": "Print the full (unpaginated) prospect list",
    "prospects.edit": "Edit organization and contact info",
    "pipeline.edit": "Move prospects between pipeline stages",
    "calls.view": "View the call log and call scripts",
    "calls.log": "Record calls",
    "calendar.view": "View the follow-up calendar and .ics feed",
    "campaigns.view": "View campaign configuration",
    "emails.view": "View sent emails, including full bodies",
    "templates.view": "View and preview email templates",
    "templates.edit": "Edit email templates",
    "portals.manage": "Create, renew and check prospects' u9itus demo pages",
    "packages.view": "Browse x402 lead packages from approved providers",
    "packages.buy": "Unlock lead packages into a campaign (spends USDC, within your allowance)",
    "spend.view": "View lead-package spending and payment receipts",
}

# Starter roles are created once if missing. Owners may edit or delete them
# afterwards; re-running the installer never overwrites those edits.
STARTER_ROLES: dict[str, tuple[str, list[str]]] = {
    "Caller": (
        "Works the phones: views prospects and scripts, logs calls, moves stages",
        ["dashboard.view", "prospects.view", "pipeline.edit",
         "calls.view", "calls.log", "calendar.view"],
    ),
    "Sales Rep": (
        "Caller access plus editing prospects and reading sent email",
        ["dashboard.view", "prospects.view", "prospects.export", "prospects.edit",
         "pipeline.edit", "calls.view", "calls.log", "calendar.view",
         "campaigns.view", "emails.view", "templates.view", "portals.manage",
         "packages.view", "spend.view"],
    ),
    "Template Editor": (
        "Writes and edits outreach email templates",
        ["dashboard.view", "campaigns.view", "templates.view", "templates.edit"],
    ),
    "Viewer": (
        "Read-only access to everything except sent email bodies",
        ["dashboard.view", "prospects.view", "calls.view", "calendar.view",
         "campaigns.view", "templates.view"],
    ),
}

# ── Route → permission map ─────────────────────────────────────────────
# Keyed by "METHOD /path/template" exactly as declared in web/app.py.
# Values are a catalog permission or one of these sentinels:

PUBLIC = "@public"   # no login (login page, health check)
ANY_USER = "@user"   # any signed-in active user (own account, logout)
OWNER = "@owner"     # owners only (user/role administration)

ROUTE_RULES: dict[str, str] = {
    "GET /healthz": PUBLIC,
    "GET /login": PUBLIC,
    "POST /login": PUBLIC,
    "POST /logout": ANY_USER,
    "GET /account": ANY_USER,
    "POST /account/password": ANY_USER,
    "GET /welcome/{token}": PUBLIC,   # one-time link; the token is the credential
    "POST /welcome/{token}": PUBLIC,

    "GET /": "dashboard.view",
    "GET /api/stats": "dashboard.view",
    "POST /prospects/saved-lists": "prospects.view",
    "POST /prospects/saved-lists/{list_id}/delete": "prospects.view",
    "GET /prospects": "prospects.view",  # print=1 additionally needs prospects.export
    "GET /prospects/{prospect_id}": "prospects.view",
    "POST /prospects/{prospect_id}/stage": "pipeline.edit",
    "POST /prospects/{prospect_id}/contact": "prospects.edit",
    "POST /prospects/{prospect_id}/info": "prospects.edit",
    "POST /prospects/{prospect_id}/portal": "portals.manage",
    "GET /calendar": "calendar.view",
    "GET /calendar.ics": "calendar.view",
    "GET /email-templates": "templates.view",
    "GET /email-templates/preview/{script_idx}": "templates.view",
    "POST /email-templates/save": "templates.edit",
    "GET /call-log": "calls.view",
    "POST /call-log/record": "calls.log",
    "GET /call-scripts": "calls.view",
    "GET /campaigns": "campaigns.view",
    "GET /emails": "emails.view",

    "GET /admin/users": OWNER,
    "POST /admin/users": OWNER,
    "POST /admin/users/{user_id}": OWNER,
    "GET /admin/roles": OWNER,
    "POST /admin/roles": OWNER,
    "POST /admin/roles/{role_id}": OWNER,
    "POST /admin/roles/{role_id}/delete": OWNER,
    "GET /admin/audit": OWNER,
    "GET /admin/jobs": OWNER,
    "POST /admin/jobs/{job_key}/run": OWNER,
    "GET /admin/campaigns": OWNER,
    "GET /admin/campaigns/{campaign_slug}": OWNER,
    "POST /admin/campaigns/{campaign_slug}": OWNER,
    "GET /plugins": "campaigns.view",
    "POST /plugins/u9itus_voter_guide/test": "portals.manage",
    "GET /mail-templates": "templates.view",
    "POST /mail-templates/save": "templates.edit",
    "GET /mail-templates/preview/{script_idx}": "templates.view",
    "GET /lead-packages": "packages.view",
    "GET /lead-packages/review": "packages.buy",
    "POST /lead-packages/unlock": "packages.buy",
    "GET /lead-packages/unlocked/{lead_package_id}": "spend.view",
    "POST /lead-packages/unlocked/{lead_package_id}/verify": "packages.buy",
    "POST /lead-packages/unlocked/{lead_package_id}/claim": "packages.buy",
    "POST /prospects/{prospect_id}/contact-event": "prospects.edit",
    "POST /prospects/{prospect_id}/refresh-email": "prospects.edit",
}

# Pages in nav order — used to pick a landing page the user can actually open.
LANDING_PAGES: list[tuple[str, str]] = [
    ("/", "dashboard.view"),
    ("/prospects", "prospects.view"),
    ("/call-scripts", "calls.view"),
    ("/calendar", "calendar.view"),
    ("/campaigns", "campaigns.view"),
    ("/email-templates", "templates.view"),
    ("/emails", "emails.view"),
]


# ── Current user ───────────────────────────────────────────────────────


@dataclass(frozen=True)
class CurrentUser:
    id: int
    email: str
    name: str
    roles: tuple[str, ...] = ()
    permissions: frozenset[str] = field(default_factory=frozenset)

    @property
    def is_owner(self) -> bool:
        return OWNER_ROLE in self.roles

    def can(self, permission: str) -> bool:
        """True if this user holds a catalog permission (owners hold all).

        Unknown permission names are always denied, even for owners, so a
        typo can never become an accidental grant.
        """
        if permission not in CATALOG:
            return False
        return self.is_owner or permission in self.permissions

    def allows(self, rule: str) -> bool:
        """Evaluate a ROUTE_RULES value for this user."""
        if rule in (PUBLIC, ANY_USER):
            return True
        if rule == OWNER:
            return self.is_owner
        return self.can(rule)

    def landing_page(self) -> str:
        for path, permission in LANDING_PAGES:
            if self.can(permission):
                return path
        return "/account"


def rule_for(method: str, path_template: str) -> str | None:
    return ROUTE_RULES.get(f"{method} {path_template}")


# ── Passwords & session tokens ─────────────────────────────────────────

_SCRYPT_N, _SCRYPT_R, _SCRYPT_P = 2**14, 8, 1
MIN_PASSWORD_LENGTH = 10


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P)
    return f"scrypt${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt_hex, digest_hex = stored.split("$")
        if scheme != "scrypt":
            return False
        digest = hashlib.scrypt(
            password.encode(), salt=bytes.fromhex(salt_hex),
            n=int(n), r=int(r), p=int(p),
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(digest.hex(), digest_hex)


def password_problem(password: str) -> str | None:
    """Return a human-readable reason the password is unacceptable, or None."""
    if len(password) < MIN_PASSWORD_LENGTH:
        return f"Password must be at least {MIN_PASSWORD_LENGTH} characters."
    return None


def new_session_token() -> str:
    return secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    """Sessions are stored by token hash so a leaked DB can't hijack logins."""
    return hashlib.sha256(token.encode()).hexdigest()
