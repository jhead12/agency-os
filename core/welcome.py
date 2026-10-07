"""
Welcome emails for new dashboard users.

The email carries a one-time "set your password" link (/welcome/<token>)
instead of a password, so no credential ever sits in an inbox. Only the
token's hash is stored; the link works once and expires after INVITE_TTL_DAYS.

Password-reset emails ("Forgot password?" on the sign-in page) use the same
one-time links, at /reset-password/<token>, and expire after RESET_TTL_HOURS.

Sent over SMTP (SMTP_HOST, SMTP_USER, SMTP_PASS, SMTP_FROM). The link
points at AGENCY_OS_BASE_URL, falling back to Railway's public domain.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta

from core import access
from core.access import CurrentUser
from core.db import Database
from core.models import SendResult

INVITE_TTL_DAYS = 7
RESET_TTL_HOURS = 1


def base_url() -> str:
    """Public URL of the dashboard, or "" if it can't be determined."""
    url = os.environ.get("AGENCY_OS_BASE_URL", "").strip()
    if not url and os.environ.get("RAILWAY_PUBLIC_DOMAIN"):
        url = f"https://{os.environ['RAILWAY_PUBLIC_DOMAIN']}"
    return url.rstrip("/")


def issue_invite(db: Database, user_id: int, site_url: str,
                 actor: CurrentUser | None = None) -> tuple[str, datetime]:
    """Create a fresh one-time link for a user. Returns (link, expires_at)."""
    token = access.new_session_token()
    expires_at = db.create_invite(user_id, access.hash_token(token),
                                  timedelta(days=INVITE_TTL_DAYS), actor)
    return f"{site_url}/welcome/{token}", expires_at


def issue_reset(db: Database, user_id: int, site_url: str) -> tuple[str, datetime]:
    """Create a one-time password-reset link. Returns (link, expires_at)."""
    token = access.new_session_token()
    expires_at = db.create_invite(user_id, access.hash_token(token),
                                  timedelta(hours=RESET_TTL_HOURS), None,
                                  action="user.password_reset_requested")
    return f"{site_url}/reset-password/{token}", expires_at


def compose(user: CurrentUser, link: str, expires_at: datetime, site_url: str) -> tuple[str, str]:
    """Return (subject, body) for a user's welcome email."""
    first_name = user.name.split()[0] if user.name.strip() else user.email.split("@")[0]
    if user.is_owner:
        access_lines = ["- Full access, including managing the team"]
    else:
        access_lines = [f"- {label}" for key, label in access.CATALOG.items() if user.can(key)]
    roles = ", ".join(user.roles) or "no role yet"

    body = f"""Hi {first_name},

You've been added to the agency-os sales dashboard ({roles}).

Set your password to get started:
{link}

This link works once and expires {expires_at:%b %d, %Y}. After that, sign in at
{site_url}/login with {user.email}.

"""
    if access_lines:
        body += "What you can do:\n" + "\n".join(access_lines) + "\n\n"
    else:
        body += "An owner still needs to assign you a role before you can see any pages.\n\n"
    body += "Reply to this email if you have any trouble getting in.\n"
    return "Welcome to agency-os — set up your account", body


def compose_reset(user: CurrentUser, link: str, site_url: str) -> tuple[str, str]:
    """Return (subject, body) for a password-reset email."""
    first_name = user.name.split()[0] if user.name.strip() else user.email.split("@")[0]
    body = f"""Hi {first_name},

Someone asked to reset the password for {user.email} on the agency-os dashboard.

Choose a new password here:
{link}

This link works once and expires in {RESET_TTL_HOURS} hour{"" if RESET_TTL_HOURS == 1 else "s"}. Setting a new
password signs you out everywhere else.

If you didn't ask for this, ignore this email; your password stays the same.
Sign in at {site_url}/login.
"""
    return "Reset your agency-os password", body


def send(to_email: str, subject: str, body: str) -> SendResult:
    """Send via the SMTP channel. Never raises; check the result's status."""
    from plugins.channels.email_smtp import EmailSmtpChannel

    return EmailSmtpChannel().send({"email": to_email}, subject, body, {})


def smtp_configured() -> bool:
    from plugins.channels.email_smtp import EmailSmtpChannel

    return EmailSmtpChannel().is_configured()
