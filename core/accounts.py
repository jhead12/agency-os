"""
Customer accounts: u9itus customers who pay to run prospect searches.

u9itus sells the plans and does the billing (docs/U9ITUS_BILLING.md). agency-os
keeps an account per paying customer, keyed by u9itus's own id for it
(external_ref), and serves /api/v1 to it:

- The platform key (`aos_plat_...`) is u9itus's: it creates accounts, issues
  their keys and suspends them. Only its SHA-256 hash is configured, in
  AGENCY_OS_PLATFORM_KEY_HASH; without it the account API answers 503.
- An account key (`aos_acct_...`) acts for one account. Only its hash is stored;
  a new one replaces the old, which stops working at once.
- A prospect an account's search finds is linked to that account
  (account_prospects). If nobody had it before, it also carries the account's id
  (prospects.account_id), and house views never show it (Database.hidden_clause).
"""

from __future__ import annotations

import hmac
import os
import re
import secrets
from typing import Optional

from core.access import hash_token
from core.models import Prospect

ACCOUNT_KEY_PREFIX = "aos_acct_"
PLATFORM_KEY_PREFIX = "aos_plat_"
STATUSES = ("active", "suspended")
MAX_PAGE = 200
_REF = re.compile(r"^[A-Za-z0-9._:-]{1,64}$")


class AccountError(ValueError):
    """A request about an account that can't be carried out; the message says why."""


# ── Keys ───────────────────────────────────────────────────────────────


def new_platform_key() -> tuple[str, str]:
    """A new platform key and the hash to set as AGENCY_OS_PLATFORM_KEY_HASH."""
    key = f"{PLATFORM_KEY_PREFIX}{secrets.token_urlsafe(32)}"
    return key, hash_token(key)


def platform_configured() -> bool:
    return bool(os.environ.get("AGENCY_OS_PLATFORM_KEY_HASH", "").strip())


def platform_key_ok(key: str) -> bool:
    expected = os.environ.get("AGENCY_OS_PLATFORM_KEY_HASH", "").strip().lower()
    key = (key or "").strip()
    return bool(expected and key.startswith(PLATFORM_KEY_PREFIX)
                and hmac.compare_digest(hash_token(key), expected))


def _new_account_key() -> str:
    return f"{ACCOUNT_KEY_PREFIX}{secrets.token_urlsafe(32)}"


# ── Accounts ───────────────────────────────────────────────────────────


def public(row) -> dict:
    """What the API and CLI show of an account (never the key hash)."""
    return {
        "external_ref": row["external_ref"], "name": row["name"], "status": row["status"],
        "key_hint": row["key_hint"], "created_at": row["created_at"], "last_used_at": row["last_used_at"],
    }


def get(db, external_ref: str) -> Optional[dict]:
    row = db.conn.execute("SELECT * FROM accounts WHERE external_ref = ?", ((external_ref or "").strip(),)).fetchone()
    return dict(row) if row else None


def list_accounts(db) -> list[dict]:
    rows = db.conn.execute(
        """SELECT a.*, (SELECT COUNT(*) FROM account_prospects ap WHERE ap.account_id = a.id) AS prospects
           FROM accounts a ORDER BY a.created_at, a.id""").fetchall()
    return [dict(r) for r in rows]


def create(db, external_ref: str, name: str) -> tuple[dict, Optional[str]]:
    """Make an account and return it with its key (shown once).

    Idempotent: an account that already exists is returned with no key (rotate_key
    issues a new one), so a retried request from u9itus never makes a second."""
    external_ref = (external_ref or "").strip()
    name = (name or "").strip()[:120]
    if not _REF.match(external_ref):
        raise AccountError("external_ref must be 1-64 letters, digits, '.', '_', ':' or '-'.")
    if not name:
        raise AccountError("name is required.")
    key = _new_account_key()
    with db.transaction() as c:
        row = c.execute(
            """INSERT INTO accounts (external_ref, name, key_hash, key_hint) VALUES (?, ?, ?, ?)
               ON CONFLICT (external_ref) DO NOTHING RETURNING *""",
            (external_ref, name, hash_token(key), key[-4:]),
        ).fetchone()
        if row is None:
            return dict(c.execute("SELECT * FROM accounts WHERE external_ref = ?", (external_ref,)).fetchone()), None
        db._audit(c, None, "account.create", "account", row["id"], {"external_ref": external_ref, "name": name})
    return dict(row), key


def rotate_key(db, external_ref: str) -> str:
    """Issue a new key for an account; the old one stops working."""
    key = _new_account_key()
    with db.transaction() as c:
        row = c.execute(
            """UPDATE accounts SET key_hash = ?, key_hint = ?, updated_at = CURRENT_TIMESTAMP
               WHERE external_ref = ? RETURNING id""",
            (hash_token(key), key[-4:], (external_ref or "").strip()),
        ).fetchone()
        if row is None:
            raise AccountError("No such account.")
        db._audit(c, None, "account.rotate_key", "account", row["id"])
    return key


def set_status(db, external_ref: str, status: str) -> dict:
    """Suspend an account (u9itus: a payment failed) or make it active again.
    A suspended account's key is refused, but its prospects are kept."""
    if status not in STATUSES:
        raise AccountError(f"status must be one of: {', '.join(STATUSES)}.")
    with db.transaction() as c:
        row = c.execute(
            """UPDATE accounts SET status = ?, updated_at = CURRENT_TIMESTAMP
               WHERE external_ref = ? RETURNING *""",
            (status, (external_ref or "").strip()),
        ).fetchone()
        if row is None:
            raise AccountError("No such account.")
        db._audit(c, None, f"account.{status}", "account", row["id"])
    return dict(row)


def for_key(db, key: str) -> Optional[dict]:
    """The account an account key belongs to (active or not), or None."""
    key = (key or "").strip()
    if not key.startswith(ACCOUNT_KEY_PREFIX):
        return None
    row = db.conn.execute("SELECT * FROM accounts WHERE key_hash = ?", (hash_token(key),)).fetchone()
    if row is None:
        return None
    db.conn.execute("UPDATE accounts SET last_used_at = CURRENT_TIMESTAMP WHERE id = ?", (row["id"],))
    db.conn.commit()
    return dict(row)


# ── An account's prospects ─────────────────────────────────────────────


def add_prospect(db, account_id: int, prospect: Prospect) -> int:
    """Save a prospect an account's search found and link it to the account."""
    c = db.conn
    with c.raw.transaction():
        prospect_id = db.upsert_prospect(prospect, account_id=account_id)
        c.execute(
            "INSERT INTO account_prospects (account_id, prospect_id) VALUES (?, ?) ON CONFLICT DO NOTHING",
            (account_id, prospect_id),
        )
    return prospect_id


def prospects(db, account_id: int, *, after: int = 0, limit: int = 50) -> list[dict]:
    """An account's prospects in id order; pass the last id as `after` for the next page."""
    limit = max(1, min(int(limit or 50), MAX_PAGE))
    rows = db.conn.execute(
        """SELECT p.id, p.name, p.website_url, p.address, p.city, p.state, p.zip, p.county,
                  p.focus_area, p.source, p.source_url, ap.added_at
           FROM account_prospects ap JOIN prospects p ON p.id = ap.prospect_id
           WHERE ap.account_id = ? AND p.id > ? ORDER BY p.id LIMIT ?""",
        (account_id, int(after or 0), limit),
    ).fetchall()
    return [dict(r) for r in rows]
