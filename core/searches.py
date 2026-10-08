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
  and the runner ends it after the current page.

Search types are plugins in plugins/searches/<name>.py (task B3):

    class CitySearch:
        key = "city"                       # the search's `type`
        def validate(self, params: dict) -> dict: ...   # cleaned params, or ValueError (→ 422)
        def run(self, params: dict, ctx: SearchContext) -> Iterator[Prospect]: ...

run() yields prospects and reports what it used with ctx.count(); the runner
saves them, stops at max_results or when cancelled, and records the usage.
SearchRunner works through queued searches: in the web app when
AGENCY_OS_RUN_SEARCHES=1, or once with `agency-os searches run`.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

import httpx

from core import accounts
from core.safe_fetch import SafeFetcher
from core.db import Database
from core.models import Prospect
from core.registry import PluginRegistry

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
MAX_RUNNING_PER_ACCOUNT = 2
STALE_MINUTES = 10           # a running search with no heartbeat for this long was interrupted
PLUGINS_DIR = Path(__file__).resolve().parent.parent / "plugins"
USER_AGENT = "agency-os prospect search (+https://u9itus.com)"
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


_plugins: dict[str, Any] | None = None


def plugins() -> dict[str, Any]:
    """The search types in plugins/searches/, found once per process."""
    global _plugins
    if _plugins is None:
        registry = PluginRegistry()
        registry.discover(str(PLUGINS_DIR), categories=("searches",))
        _plugins = {k: v for k, v in registry.searches.items()
                    if k in TYPES and callable(getattr(v, "validate", None)) and callable(getattr(v, "run", None))}
    return _plugins


def create(db, account_id: int, body: dict, available: Optional[dict] = None) -> tuple[dict, bool]:
    """Queue a search for an account. Returns (search, created); created is False
    when this idempotency_key already made the same search. With `available`
    (search type → plugin), the type must have a plugin and its validate() runs."""
    kind, params, max_results, key = validate(body)
    if available is not None:
        plugin = available.get(kind)
        if plugin is None:
            raise SearchError(f"{kind} searches aren't available yet.")
        try:
            params = plugin.validate(params)
        except ValueError as e:
            raise SearchError(str(e)) from None
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


# ── Running searches ───────────────────────────────────────────────────


@dataclass
class SearchContext:
    """What a search plugin gets for one run."""
    search: dict
    db: Database
    http: httpx.Client
    delivered: int = 0
    usage: dict = field(default_factory=lambda: {"pages_fetched": 0, "api_requests": 0, "cost_cents": 0})
    transport: Optional[httpx.BaseTransport] = None   # for the guarded fetcher; tests fake it
    is_cancelled: Optional[Callable[[], bool]] = None  # other callers (core/generator.py) bring their own
    _fetch: Optional[SafeFetcher] = None

    @property
    def fetch(self) -> SafeFetcher:
        """The only way a search may fetch a URL the customer gave (core/safe_fetch.py)."""
        if self._fetch is None:
            self._fetch = SafeFetcher(transport=self.transport, user_agent=USER_AGENT)
        return self._fetch

    def close(self) -> None:
        if self._fetch is not None:
            self._fetch.close()

    @property
    def remaining(self) -> int:
        """How many more prospects this search may deliver."""
        return max(0, self.search["max_results"] - self.delivered)

    def count(self, *, pages_fetched: int = 0, api_requests: int = 0, cost_cents: int = 0) -> None:
        """Record what the search used (our cost, shown to u9itus as usage)."""
        self.usage["pages_fetched"] += pages_fetched
        self.usage["api_requests"] += api_requests
        self.usage["cost_cents"] += cost_cents

    def cancelled(self) -> bool:
        if self.is_cancelled is not None:
            return self.is_cancelled()
        row = self.db.conn.execute("SELECT cancel_requested_at FROM searches WHERE id = ?",
                                   (self.search["id"],)).fetchone()
        return row is None or row["cancel_requested_at"] is not None


def runs_enabled() -> bool:
    return os.environ.get("AGENCY_OS_RUN_SEARCHES", "").strip().lower() in ("1", "true", "yes", "on")


class SearchRunner:
    """Claims queued searches and runs them, one at a time per runner."""

    def __init__(self, db_url: Optional[str] = None, available: Optional[dict] = None,
                 transport: Optional[httpx.BaseTransport] = None):
        self.db_url = db_url
        self.available = available
        self.transport = transport

    def _plugins(self) -> dict:
        return self.available if self.available is not None else plugins()

    def reap_stale(self, db: Database) -> int:
        """Fail searches left running by a restart; what they delivered is still billed."""
        rows = db.conn.execute(
            f"""UPDATE searches SET status = 'failed', finished_at = CURRENT_TIMESTAMP,
                   error = 'The search was interrupted. Results found before then are kept.'
                WHERE status = 'running'
                  AND COALESCE(heartbeat_at, started_at) < CURRENT_TIMESTAMP - INTERVAL '{STALE_MINUTES} minutes'
                RETURNING id""").fetchall()
        return len(rows)

    def claim(self, db: Database) -> Optional[dict]:
        """Take the oldest queued search of an active account that has room to run one."""
        row = db.conn.execute(
            """UPDATE searches SET status = 'running', started_at = CURRENT_TIMESTAMP,
                      heartbeat_at = CURRENT_TIMESTAMP
               WHERE id = (
                   SELECT s.id FROM searches s JOIN accounts a ON a.id = s.account_id
                   WHERE s.status = 'queued' AND a.status = 'active'
                     AND (SELECT COUNT(*) FROM searches r
                          WHERE r.account_id = s.account_id AND r.status = 'running') < ?
                   ORDER BY s.created_at, s.id LIMIT 1 FOR UPDATE OF s SKIP LOCKED)
               RETURNING *""",
            (MAX_RUNNING_PER_ACCOUNT,),
        ).fetchone()
        return dict(row) if row else None

    def run_one(self, db: Database, search: dict) -> dict:
        """Run one claimed search to the end. Never raises; returns the finished search."""
        plugin = self._plugins().get(search["type"])
        status, error = "done", None
        with httpx.Client(timeout=90, transport=self.transport, follow_redirects=True,
                          headers={"User-Agent": USER_AGENT}) as http:
            ctx = SearchContext(search, db, http, transport=self.transport)
            try:
                if plugin is None:
                    raise ValueError(f"{search['type']} searches aren't available.")
                params = plugin.validate(json.loads(search["params"]))
                for prospect in plugin.run(params, ctx):
                    if ctx.cancelled():
                        status = "canceled"
                        break
                    saved = save_result(db, search, prospect)
                    if saved is None:
                        break
                    ctx.delivered += int(saved)
                    db.conn.execute(
                        """UPDATE searches SET heartbeat_at = CURRENT_TIMESTAMP, pages_fetched = ?,
                                  api_requests = ?, cost_cents = ? WHERE id = ?""",
                        (*ctx.usage.values(), search["id"]))
                    if ctx.remaining == 0:
                        break
                else:
                    if ctx.cancelled():
                        status = "canceled"
            except ValueError as e:  # the plugin's own message, safe to show the customer
                status, error = "failed", str(e)[:500]
            except Exception as e:  # a bug or an outage; details go to the log, not the customer
                print(f"agency-os: search {search['id']} failed: {type(e).__name__}: {e}\n"
                      f"{traceback.format_exc(limit=5)}")
                status, error = "failed", "The search failed. Results found before then are kept."
            finally:
                ctx.close()
        row = db.conn.execute(
            """UPDATE searches SET status = ?, error = ?, finished_at = CURRENT_TIMESTAMP,
                      heartbeat_at = CURRENT_TIMESTAMP, pages_fetched = ?, api_requests = ?, cost_cents = ?
               WHERE id = ? RETURNING *""",
            (status, error, *ctx.usage.values(), search["id"]),
        ).fetchone()
        return dict(row)

    def run_pending(self, limit: int = 20) -> list[dict]:
        """Run queued searches until none are left (or `limit` ran). Returns the finished searches."""
        db = Database(self.db_url)
        self.reap_stale(db)
        finished = []
        while len(finished) < limit:
            search = self.claim(db)
            if search is None:
                break
            finished.append(self.run_one(db, search))
        return finished

    async def loop(self, check_every_seconds: int = 5) -> None:
        """Run queued searches forever, in a worker thread so requests aren't blocked."""
        while True:
            try:
                await asyncio.to_thread(self.run_pending)
            except Exception as e:  # e.g. the database is briefly unreachable; try again next time
                print(f"agency-os: search runner: {type(e).__name__}: {e}")
            await asyncio.sleep(check_every_seconds)
