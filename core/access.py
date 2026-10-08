"""
Users, roles, and permissions for agency-os.

Modeled on the u9itus.dev staff permission system:

- Permissions are a fixed, code-defined catalog. Owners create named roles
  by picking from it; a role name alone never grants anything.
- The protected Owner role bypasses permission checks and is the only role
  that can manage users, roles, and the audit log.
- The protected Super Admin role sits above Owner: it has every Owner power,
  and only a Super Admin can grant or remove Owner or Super Admin, or change
  another Owner's or a Super Admin's account (name, active, password). (The
  server-side CLI can too, for bootstrap/recovery.)
- AI agents: an account marked as an AI agent never holds Owner or Super
  Admin and never gets AGENT_DENIED permissions, whatever its roles say. Any
  account's tool calls through an autonomous channel (an AI connector, the
  in-page assistant, a CLI key) are recorded on it, so the Team page shows
  who is acting through AI.
- Every web route must appear in ROUTE_RULES. Unlisted routes are denied.
- New users get no access until an owner assigns them a role.

This module holds policy only (no SQL). Persistence lives in core/db.py.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
from dataclasses import dataclass, field

OWNER_ROLE = "Owner"
SUPER_ADMIN_ROLE = "Super Admin"
PROTECTED_ROLES = (SUPER_ADMIN_ROLE, OWNER_ROLE)


def ai_allowed() -> bool:
    """Owner-wide kill switch: AGENCY_OS_AI=off hides every AI feature for everyone."""
    return os.environ.get("AGENCY_OS_AI", "").strip().lower() not in ("off", "0", "false", "no")


class AccessError(ValueError):
    """A user/role change was rejected (invalid input or a broken invariant)."""

# ── AI agents ──────────────────────────────────────────────────────────

# What an AI agent account can't do even if a role grants it: spend or sell, or run the console.
AGENT_DENIED = frozenset({"packages.buy", "packages.sell", "cli.use"})
# Inbox providers made for AI agents; a new user at one is marked as an AI agent.
AGENT_EMAIL_DOMAINS = ("agentmail.to",)


def looks_like_agent_email(email: str) -> bool:
    domain = email.strip().lower().rpartition("@")[2]
    return any(domain == d or domain.endswith("." + d) for d in AGENT_EMAIL_DOMAINS)


def autonomous_channel(source: str) -> str:
    """The autonomous channel a tool call came through, or "" for a person in the app.

    "ai" is an AI connector (MCP) or the in-page assistant (WebMCP); "key" is a CLI key.
    The agent panel and the browser console are a person clicking or typing.
    """
    if source.startswith("mcp:") or source == "webmcp":
        return "ai"
    return "key" if source == "cli" else ""


def describe_channel(source: str) -> str:
    if source.startswith("mcp:"):
        return f"AI connector ({source.removeprefix('mcp:')})"
    return {"webmcp": "in-page AI assistant", "cli": "CLI key"}.get(source, source)

# ── Outreach channels ──────────────────────────────────────────────────

# Channels that spend money on the one shared provider account (Lob: physical
# mail). Only a Super Admin turns them on or off for a campaign, until the
# costs and liabilities are worked out for anyone else.
SUPER_ADMIN_CHANNELS = frozenset({"lob_direct_mail"})


def channel_change_problem(user: "CurrentUser", before, after) -> str | None:
    """Why `user` can't change a campaign's channels from `before` to `after`, or None."""
    if user.is_super_admin:
        return None
    changed = sorted((set(before) ^ set(after)) & SUPER_ADMIN_CHANNELS)
    if changed:
        return f"Only a Super Admin can turn {', '.join(changed)} on or off for a campaign."
    return None

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
    "portals.manage": "Create, renew and check prospects' demo pages",
    "packages.view": "Browse x402 lead packages from approved providers",
    "packages.buy": "Unlock lead packages into a campaign (spends USDC, within your allowance)",
    "spend.view": "View lead-package spending and payment receipts",
    "agents.use": "Run the built-in AI agents (drafts only; nothing is sent)",
    "ai.connect": "Connect your own AI assistant to agency-os (WebMCP)",
    "packages.sell": "Publish our own lists as lead packages, and handle buyers' claims and refunds",
    "royalties.view_own": "See your own data royalties and set where they're paid",
    "recruiting.view": "See recruiting campaigns and their leads (e.g. attorneys), and buy lists into them",
    "cli.use": "Use the command console (in the browser and the remote CLI); commands still need their own permissions",
}

# Starter roles are created once if missing. Owners may edit or delete them
# afterwards; re-running the installer never overwrites those edits.
STARTER_ROLES: dict[str, tuple[str, list[str]]] = {
    "Caller": (
        "Works the phones: views prospects and scripts, logs calls, moves stages",
        ["dashboard.view", "prospects.view", "pipeline.edit",
         "calls.view", "calls.log", "calendar.view", "royalties.view_own"],
    ),
    "Sales Rep": (
        "Caller access plus editing prospects and reading sent email",
        ["dashboard.view", "prospects.view", "prospects.export", "prospects.edit",
         "pipeline.edit", "calls.view", "calls.log", "calendar.view",
         "campaigns.view", "emails.view", "templates.view", "portals.manage",
         "packages.view", "spend.view", "agents.use", "ai.connect", "royalties.view_own"],
    ),
    "Recruiter": (
        "Works recruiting campaigns (e.g. attorneys): calls, emails, and buys lead lists into them",
        ["dashboard.view", "prospects.view", "prospects.export", "prospects.edit",
         "pipeline.edit", "calls.view", "calls.log", "calendar.view",
         "campaigns.view", "emails.view", "templates.view",
         "packages.view", "packages.buy", "spend.view", "royalties.view_own", "recruiting.view"],
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
SUPER_ADMIN = "@super_admin"  # super admins only (creating and promoting owners)

ROUTE_RULES: dict[str, str] = {
    "GET /healthz": PUBLIC,
    "GET /login": PUBLIC,
    "POST /login": PUBLIC,
    "POST /logout": ANY_USER,
    "GET /account": ANY_USER,
    "POST /account/password": ANY_USER,
    "POST /account/ai": ANY_USER,
    "POST /account/tokens": ANY_USER,
    "POST /account/tokens/{token_id}/revoke": ANY_USER,
    "GET /oauth/consent": ANY_USER,
    "POST /oauth/consent": ANY_USER,
    # Provider webhooks: no login; each checks a signature or a secret key (core/evidence.py).
    "POST /webhooks/lob": PUBLIC,
    "POST /webhooks/smartlead": PUBLIC,
    "POST /webhooks/bounce": PUBLIC,
    "POST /webhooks/voice/{provider_key}/dial": PUBLIC,    # signed by the provider (core/voice.py)
    "POST /webhooks/voice/{provider_key}/status": PUBLIC,
    # Browser calling: Super Admins only until dialing opens to Owners (docs/BROWSER_CALLING.md, V8).
    "GET /voice/token": SUPER_ADMIN,
    "POST /voice/calls/{voice_call_id}/disclosure": SUPER_ADMIN,  # and only the rep who placed the call
    "GET /voice/calls/{provider_key}/{call_sid}": SUPER_ADMIN,   # the rep's own call: status after hang-up
    # Selling: the x402 provider endpoints are public; payment or a claim token authorizes them.
    "GET /x402/packages": PUBLIC,
    "GET /x402/packages/{slug}/leads": PUBLIC,
    "POST /x402/packages/{slug}/contacts": PUBLIC,
    "POST /x402/packages/{slug}/claims": PUBLIC,
    # Customer accounts (core/accounts.py): the platform key or an account key authorizes them.
    "POST /api/v1/accounts": PUBLIC,
    "POST /api/v1/accounts/{external_ref}/key": PUBLIC,
    "POST /api/v1/accounts/{external_ref}/status": PUBLIC,
    "GET /api/v1/account": PUBLIC,
    "GET /api/v1/prospects": PUBLIC,
    "POST /api/v1/searches": PUBLIC,
    "GET /api/v1/searches/{search_id}": PUBLIC,
    "POST /api/v1/searches/{search_id}/cancel": PUBLIC,
    "GET /admin/selling": "packages.sell",
    "POST /admin/selling/publish": "packages.sell",
    "POST /admin/selling/{package_id}/active": "packages.sell",
    "POST /admin/selling/claims/{claim_id}/refund": "packages.sell",
    "POST /prospects/{prospect_id}/do-not-sell": "prospects.edit",
    "POST /prospects/{prospect_id}/do-not-call": "prospects.edit",
    "POST /prospects/{prospect_id}/credit": "packages.sell",
    "POST /account/payout-address": "royalties.view_own",
    # Lead package generator (core/generator.py): runs searches and enrichers on the house's dime.
    "GET /admin/generator": SUPER_ADMIN,
    "POST /admin/generator": SUPER_ADMIN,
    "GET /admin/generator/{run_id}": SUPER_ADMIN,
    "POST /admin/generator/{run_id}/cancel": SUPER_ADMIN,
    # Customer accounts (core/accounts.py, docs/U9ITUS_BILLING.md task B8).
    "GET /admin/accounts": OWNER,
    "POST /admin/accounts/{external_ref}/status": OWNER,
    "POST /admin/accounts/{external_ref}/key": OWNER,
    "GET /admin/payouts": OWNER,
    "POST /admin/payouts/{user_id}": OWNER,
    "GET /welcome/{token}": PUBLIC,   # one-time link; the token is the credential
    "POST /welcome/{token}": PUBLIC,
    # CAN-SPAM opt-out (core/compliance.py): the signed token in the link is the credential.
    "GET /unsubscribe": PUBLIC,
    "POST /unsubscribe": PUBLIC,
    "GET /forgot-password": PUBLIC,
    "POST /forgot-password": PUBLIC,  # rate-limited; same answer whether or not the email exists
    "GET /reset-password/{token}": PUBLIC,  # one-time link; the token is the credential
    "POST /reset-password/{token}": PUBLIC,

    "GET /": "dashboard.view",
    "GET /api/stats": "dashboard.view",
    "POST /prospects/saved-lists": "prospects.view",
    "POST /prospects/saved-lists/{list_id}/delete": "prospects.view",
    "GET /prospects": "prospects.view",  # print=1 additionally needs prospects.export
    "GET /prospects/{prospect_id}": "prospects.view",
    "GET /api/prospects/{prospect_id}/neighbors": "prospects.view",
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
    "POST /admin/campaigns/create": OWNER,
    "GET /admin/campaigns/{campaign_slug}": OWNER,
    "POST /admin/campaigns/{campaign_slug}": OWNER,
    "POST /admin/campaigns/{campaign_slug}/import-csv": OWNER,
    "POST /admin/campaigns/{campaign_slug}/members": OWNER,
    "POST /admin/campaigns/{campaign_slug}/owners": SUPER_ADMIN,
    "POST /admin/campaigns/{campaign_slug}/owners/{owner_id}/delete": SUPER_ADMIN,
    "POST /admin/campaigns/{campaign_slug}/members/{member_id}/delete": OWNER,
    "GET /admin/campaigns/{campaign_slug}/import-template": OWNER,
    "GET /plugins": "campaigns.view",
    "POST /plugins/{plugin_key}/test": "portals.manage",
    # Plugin pages: each page names its own permission, checked in web/app.py
    # through core/plugin_pages.py (a page without a valid one is denied for everyone).
    "GET /p/{page_key}": ANY_USER,
    "POST /p/{page_key}": ANY_USER,
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
    "POST /prospects/{prospect_id}/agent": "agents.use",
    "POST /prospects/{prospect_id}/agent/note": "agents.use",
    "POST /agent/chat": "agents.use",
    "GET /api/tools": "ai.connect",
    "POST /api/tools/{tool_name}": "ai.connect",
    "GET /workflows": ANY_USER,  # tutorials (filtered by permission) and the user's own workflows
    "GET /api/workflows/{source}/{slug}": ANY_USER,
    "POST /api/workflows/preview": ANY_USER,  # validates an unsaved workflow for "Try it"
    "POST /workflows/save": ANY_USER,
    "POST /workflows/import": ANY_USER,
    "GET /workflows/export": ANY_USER,
    "POST /workflows/{slug}/delete": ANY_USER,
    "POST /api/layouts/{page}": ANY_USER,  # the user's own panel arrangement (core/panels.py)
    "GET /console": "cli.use",
    "POST /api/console": "cli.use",  # also accepts a CLI key (Authorization: Bearer aos_cli_...)
    "POST /account/cli-keys": "cli.use",
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
    ai_enabled: bool = False  # the user opted in to AI features (Account page)
    restricted_campaigns: frozenset[str] = field(default_factory=frozenset)  # campaigns that have members
    member_campaigns: frozenset[str] = field(default_factory=frozenset)      # ...of which this user is one
    owner_restricted_campaigns: frozenset[str] = field(default_factory=frozenset)  # campaigns with assigned Owners
    owner_campaigns: frozenset[str] = field(default_factory=frozenset)             # ...assigned to this user
    is_agent: bool = False  # an AI agent account (Team page)

    @property
    def is_super_admin(self) -> bool:
        # The database never lets an agent hold the role; this holds even if a row says otherwise.
        return SUPER_ADMIN_ROLE in self.roles and not self.is_agent

    @property
    def is_owner(self) -> bool:
        """Owners, and Super Admins (who hold every Owner power). Never an AI agent."""
        return (OWNER_ROLE in self.roles and not self.is_agent) or self.is_super_admin

    def can(self, permission: str) -> bool:
        """True if this user holds a catalog permission (owners hold all).

        Unknown permission names are always denied, even for owners, so a
        typo can never become an accidental grant. AI agents never get
        AGENT_DENIED.
        """
        if permission not in CATALOG or (self.is_agent and permission in AGENT_DENIED):
            return False
        return self.is_owner or permission in self.permissions

    def sees_campaign(self, campaign) -> bool:
        """Whether this user sees a campaign and its leads.

        Super Admins see every campaign. Owners see every campaign except one a
        Super Admin has assigned to other Owners. Anyone else needs the
        campaign's `requires_permission`, if it has one (a misspelled permission
        hides it rather than showing it), and, once the campaign has members
        (Admin → Campaign Settings), to be one of them, directly or by role.
        """
        name = getattr(campaign, "db_name", "")
        if self.is_super_admin:
            return True
        if self.is_owner:
            return name not in self.owner_restricted_campaigns or name in self.owner_campaigns
        required = getattr(campaign, "requires_permission", "")
        if required and not self.can(required):
            return False
        return name not in self.restricted_campaigns or name in self.member_campaigns

    def uses_ai(self, permission: str) -> bool:
        """An AI feature is on for this user: allowed on the server, opted in, and permitted.

        Every AI surface checks this, so users who haven't opted in see the
        app exactly as it was.
        """
        return ai_allowed() and self.ai_enabled and self.can(permission)

    def allows(self, rule: str) -> bool:
        """Evaluate a ROUTE_RULES value for this user."""
        if rule in (PUBLIC, ANY_USER):
            return True
        if rule == OWNER:
            return self.is_owner
        if rule == SUPER_ADMIN:
            return self.is_super_admin
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
