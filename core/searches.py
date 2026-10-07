"""
An account's paid prospect searches (docs/U9ITUS_BILLING.md, task B2).

u9itus creates a search with the customer's account key, holds credits for
max_results, polls until the search is done, then charges for `delivered`:
the prospects this search first linked to the account. A prospect the
account already had is listed in the search's results but not billed again.

- idempotency_key is u9itus's own id for the search. Sending it again returns
  the same search; sending it with different settings is a conflict, so a
  retried request can never start (or bill) a second search.
- A search never delivers more than max_results (save_result refuses).
- Cancelling a queued search ends it at once; a running one is asked to stop
  and the runner (task B3) ends it after the current page.
"""

from __future__ import annotations

import json
import re
from typing import Optional

from core import accounts
from core.models import Prospect

TYPES = ("city", "rss", "scrape")
# The settings each type must have. Each search plugin checks the rest (B5-B7).
REQUIRED_PARAMS = {
    "city": ("city", "state", "query"),
    "rss": ("feed_url",),
    "scrape": ("url", "item_selector", "fields"),
}
MAX_RESULTS = 500
MAX_PARAMS_BYTES = 4096
FINISHED = ("done", "failed", "canceled")
_KEY = re.compile(r"^[A-Za-z0-9._:-]{1,64}$")


class SearchError(ValueError):
    """A search request that can't be accepted; the message says why."""


class SearchConflict(SearchError):
    """The idempotency_key was already used for a search with different settings."""


def _canonical(params: dict) -> str:
    return json.dumps(params, sort_keys=True, separators=(",", ":"))


def validate(body: dict) -> tuple[str, dict, int, str]:
    """(type, params, max_results, idempotency_key) from a create request, or SearchError."""
    kind = str(body.get("type") or "")
    if kind not in TYPES:
        raise SearchError(f"type must be one of: {', '.join(TYPES)}.")
    params = body.get("params")
    if not isinstance(params, dict):
        raise SearchError("params must be a JSON object.")
    if len(_canonical(params).encode()) > MAX_PARAMS_BYTES:
        raise SearchError(f"params must be at most {MAX_PARAMS_BYTES} bytes.")
    missing = [k for k in REQUIRED_PARAMS[kind] if params.get(k) in (None, "", [], {})]
    if missing:
        raise SearchError(f"A {kind} search needs params: {', '.join(missing)}.")
    max_results = body.get("max_results")
    if isinstance(max_results, bool) or not isinstance(max_results, int) or not 1 <= max_results <= MAX_RESULTS:
        raise SearchError(f"max_results must be a whole number from 1 to {MAX_RESULTS}.")
    key = str(body.get("idempotency_key") or "")
    if not _KEY.match(key):
        raise SearchError("idempotency_key must be 1-64 letters, digits, '.', '_', ':' or '-'.")
    return kind, params, max_results, key


def create(db, account_id: int, body: dict) -> tuple[dict, bool]:
    """Queue a search for an account. Returns (search, created); created is False
    when this idempotency_key already made the same search."""
    kind, params, max_results, key = validate(body)
    c = db.conn
    row = c.execute(
        """INSERT INTO searches (account_id, idempotency_key, type, params, max_results)
           VALUES (?, ?, ?, ?, ?) ON CONFLICT (account_id, idempotency_key) DO NOTHING RETURNING *""",
        (account_id, key, kind, _canonical(params), max_results),
    ).fetchone()
    if row is not None:
        return dict(row), True
    existing = dict(c.execute("SELECT * FROM searches WHERE account_id = ? AND idempotency_key = ?",
                              (account_id, key)).fetchone())
    if (existing["type"], existing["params"], existing["max_results"]) != (kind, _canonical(params), max_results):
        raise SearchConflict("This idempotency_key was already used for a different search.")
    return existing, False


def get(db, account_id: int, search_id: int) -> Optional[dict]:
    """One of the account's searches, or None (also for another account's)."""
    row = db.conn.execute("SELECT * FROM searches WHERE id = ? AND account_id = ?",
                          (search_id, account_id)).fetchone()
    return dict(row) if row else None


def cancel(db, account_id: int, search_id: int) -> Optional[dict]:
    """Cancel a search: a queued one ends now, a running one is asked to stop.
    A finished search is returned unchanged. None if it isn't the account's."""
    row = db.conn.execute(
        """UPDATE searches SET
             status = CASE WHEN status = 'queued' THEN 'canceled' ELSE status END,
             finished_at = CASE WHEN status = 'queued' THEN CURRENT_TIMESTAMP ELSE finished_at END,
             cancel_requested_at = COALESCE(cancel_requested_at, CURRENT_TIMESTAMP)
           WHERE id = ? AND account_id = ? AND status IN ('queued', 'running') RETURNING *""",
        (search_id, account_id),
    ).fetchone()
    return dict(row) if row else get(db, account_id, search_id)


def save_result(db, search: dict, prospect: Prospect) -> Optional[bool]:
    """Save one prospect a search found. Returns whether it was new to the account
    (billable), or None when the search has already delivered max_results."""
    c = db.conn
    with c.raw.transaction():
        # Lock the search row so concurrent saves can't pass max_results.
        delivered = c.execute("SELECT delivered FROM searches WHERE id = ? FOR UPDATE",
                              (search["id"],)).fetchone()["delivered"]
        if delivered >= search["max_results"]:
            return None
        prospect_id, is_new = accounts.link_prospect(db, search["account_id"], prospect)
        added = c.execute(
            """INSERT INTO search_results (search_id, prospect_id, is_new) VALUES (?, ?, ?)
               ON CONFLICT DO NOTHING RETURNING prospect_id""",
            (search["id"], prospect_id, int(is_new)),
        ).fetchone()
        if added is not None and is_new:
            c.execute("UPDATE searches SET delivered = delivered + 1 WHERE id = ?", (search["id"],))
    return bool(added is not None and is_new)


def public(search: dict) -> dict:
    """A search as the API shows it (docs/U9ITUS_BILLING.md, section 3)."""
    return {
        "id": search["id"], "type": search["type"], "params": json.loads(search["params"]),
        "status": search["status"], "max_results": search["max_results"], "delivered": search["delivered"],
        "usage": {"pages_fetched": search["pages_fetched"], "api_requests": search["api_requests"],
                  "cost_cents": search["cost_cents"]},
        "error": search["error"], "cancel_requested": search["cancel_requested_at"] is not None,
        "idempotency_key": search["idempotency_key"],
        "created_at": search["created_at"], "started_at": search["started_at"],
        "finished_at": search["finished_at"],
    }
