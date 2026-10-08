"""
An account's own API keys for the paid enrichers (bring your own key).

A u9itus customer who has their own Apollo, Hunter or Firecrawl plan can give
agency-os its key, so enrichment for their searches runs on their plan: they
pay a platform fee instead of the full enrichment price (docs/U9ITUS_BILLING.md,
decision 8.6).

How the keys are kept:
- Encrypted with AES-256-GCM under a master key that lives only in the
  environment (AGENCY_OS_CREDENTIALS_KEY), never in the database. A copy of
  the database or a backup alone gives nobody the keys.
- Each ciphertext is bound to its account and provider (GCM associated data),
  so a row copied onto another account or provider fails to decrypt.
- Write-only through the API: a key is never returned, only its last 4
  characters. reveal() is for the enrichment step, server side.
- Rotating the master key: put the new key in AGENCY_OS_CREDENTIALS_KEY, the
  old one in AGENCY_OS_CREDENTIALS_OLD_KEYS, run
  `agency-os accounts reencrypt-credentials`, then drop the old key.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import os
import re
import secrets
from typing import Optional

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

KEY_ENV = "AGENCY_OS_CREDENTIALS_KEY"
OLD_KEYS_ENV = "AGENCY_OS_CREDENTIALS_OLD_KEYS"
PROVIDERS = ("apollo", "hunter", "firecrawl")
_SECRET = re.compile(r"^[\x21-\x7e]{8,512}$")  # printable ASCII, no spaces


class CredentialError(ValueError):
    """A credential request that can't be carried out; the message says why."""


class NotConfigured(RuntimeError):
    """AGENCY_OS_CREDENTIALS_KEY isn't set (or isn't a valid key)."""


# ── Master keys ────────────────────────────────────────────────────────


def new_master_key() -> str:
    """A new value for AGENCY_OS_CREDENTIALS_KEY: 32 random bytes, base64."""
    return base64.b64encode(secrets.token_bytes(32)).decode()


def _decode(value: str) -> bytes:
    try:
        raw = base64.b64decode(value.strip(), validate=True)
    except (binascii.Error, ValueError):
        raw = b""
    if len(raw) != 32:
        raise NotConfigured(f"{KEY_ENV} must be 32 bytes, base64 (agency-os accounts credentials-key).")
    return raw


def _key_id(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()[:12]


def _current() -> bytes:
    value = os.environ.get(KEY_ENV, "")
    if not value.strip():
        raise NotConfigured(f"{KEY_ENV} is not set.")
    return _decode(value)


def _keyring() -> dict[str, bytes]:
    """Every master key that may decrypt: the current one and any old ones, by key id."""
    keys = [_current()] + [_decode(v) for v in os.environ.get(OLD_KEYS_ENV, "").split(",") if v.strip()]
    return {_key_id(k): k for k in keys}


def configured() -> bool:
    try:
        _current()
    except NotConfigured:
        return False
    return True


def _aad(account_id: int, provider: str) -> bytes:
    return f"agency-os:credential:v1:{int(account_id)}:{provider}".encode()


def _encrypt(account_id: int, provider: str, secret: str) -> tuple[str, str]:
    key = _current()
    nonce = secrets.token_bytes(12)
    sealed = AESGCM(key).encrypt(nonce, secret.encode(), _aad(account_id, provider))
    return base64.b64encode(nonce + sealed).decode(), _key_id(key)


def _decrypt(row: dict) -> str:
    key = _keyring().get(row["key_id"])
    if key is None:
        raise NotConfigured(f"The master key that encrypted this credential ({row['key_id']}) is not set.")
    blob = base64.b64decode(row["secret"])
    try:
        return AESGCM(key).decrypt(blob[:12], blob[12:], _aad(row["account_id"], row["provider"])).decode()
    except InvalidTag:
        raise CredentialError("A stored credential failed its integrity check.") from None


# ── An account's credentials ───────────────────────────────────────────


def public(row) -> dict:
    """What the API and CLI show of a credential: never the key."""
    return {"provider": row["provider"], "hint": row["hint"], "created_at": row["created_at"],
            "updated_at": row["updated_at"], "last_used_at": row["last_used_at"]}


def _provider(provider: str) -> str:
    provider = (provider or "").strip().lower()
    if provider not in PROVIDERS:
        raise CredentialError(f"provider must be one of: {', '.join(PROVIDERS)}.")
    return provider


def put(db, account_id: int, provider: str, secret: str, actor=None) -> dict:
    """Save (or replace) an account's key for a provider."""
    provider = _provider(provider)
    secret = (secret or "").strip()
    if not _SECRET.match(secret):
        raise CredentialError("key must be 8-512 printable characters with no spaces.")
    blob, key_id = _encrypt(account_id, provider, secret)
    with db.transaction() as c:
        row = c.execute(
            """INSERT INTO account_credentials (account_id, provider, secret, key_id, hint)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT (account_id, provider) DO UPDATE SET secret = EXCLUDED.secret,
                   key_id = EXCLUDED.key_id, hint = EXCLUDED.hint, updated_at = CURRENT_TIMESTAMP,
                   last_used_at = NULL
               RETURNING *""",
            (account_id, provider, blob, key_id, secret[-4:]),
        ).fetchone()
        db._audit(c, actor, "account.credential_set", "account", account_id, {"provider": provider})
    return dict(row)


def list_for(db, account_id: int) -> list[dict]:
    rows = db.conn.execute(
        "SELECT * FROM account_credentials WHERE account_id = ? ORDER BY provider", (account_id,)).fetchall()
    return [dict(r) for r in rows]


def delete(db, account_id: int, provider: str, actor=None) -> bool:
    """Forget an account's key for a provider. False if it had none."""
    provider = _provider(provider)
    with db.transaction() as c:
        row = c.execute("DELETE FROM account_credentials WHERE account_id = ? AND provider = ? RETURNING provider",
                        (account_id, provider)).fetchone()
        if row:
            db._audit(c, actor, "account.credential_delete", "account", account_id, {"provider": provider})
    return row is not None


def reveal(db, account_id: int, provider: str) -> Optional[str]:
    """The account's key for a provider, decrypted for an enrichment call, or None.
    Server side only: never put the result in a response, a log or an error message."""
    row = db.conn.execute("SELECT * FROM account_credentials WHERE account_id = ? AND provider = ?",
                          (account_id, _provider(provider))).fetchone()
    if row is None:
        return None
    secret = _decrypt(dict(row))
    db.conn.execute("UPDATE account_credentials SET last_used_at = CURRENT_TIMESTAMP WHERE account_id = ? "
                    "AND provider = ?", (account_id, row["provider"]))
    db.conn.commit()
    return secret


def reencrypt(db) -> int:
    """Re-encrypt every credential not under the current master key; returns how many."""
    current = _key_id(_current())
    count = 0
    with db.transaction() as c:
        rows = c.execute("SELECT * FROM account_credentials WHERE key_id <> ?", (current,)).fetchall()
        for row in rows:
            blob, key_id = _encrypt(row["account_id"], row["provider"], _decrypt(dict(row)))
            c.execute("UPDATE account_credentials SET secret = ?, key_id = ? WHERE account_id = ? AND provider = ?",
                      (blob, key_id, row["account_id"], row["provider"]))
            count += 1
    return count
