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

Search types are plugins in plugins/searches/<name>.py (task B3). Adding a
file there is all a new type needs: u9itus lists the types and builds its
form from GET /api/v1/search-types.

    class CitySearch:
        key = "city"                       # the search's `type`
        label = "Businesses in a city"     # shown to the customer
        description = "..."
        fields = [{"name": "city", "label": "City", "type": "text", "required": True, ...}]
        attribution = "© OpenStreetMap contributors, ODbL"   # or None
        def validate(self, params: dict) -> dict: ...   # cleaned params, or ValueError (→ 422)
        def run(self, params: dict, ctx: SearchContext) -> Iterator[Prospect]: ...

Usage (task B4) is read straight from the searches table, so its totals
always equal the sum of the searches. An account may have at most
AGENCY_OS_ACCOUNT_DAILY_PROSPECTS (default 2000) prospects delivered or
reserved per day; a search past that is refused with SearchLimit (429).

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
from datetime import date, timedelta
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

import httpx

from core import accounts
from core.safe_fetch import SafeFetcher
from core.db import Database
from core.models import Prospect
from core.registry import PluginRegistry

# Without plugins (tests, and callers that don't pass any) these are the known
# types and the settings each must have; with plugins, each plugin's `fields` say.
TYPES = ("city", "rss", "scrape")
REQUIRED_PARAMS = {
    "city": ("city", "state", "query"),
    "rss": ("feed_url",),
    "scrape": ("url", "item_selector", "fields"),
}
MAX_RESULTS = 500
MAX_PARAMS_BYTES = 4096
FINISHED = ("done", "failed", "canceled")
MAX_RUNNING_PER_ACCOUNT = 2
DEFAULT_DAILY_PROSPECTS = 2000
MAX_USAGE_DAYS = 366
STALE_MINUTES = 10           # a running search with no heartbeat for this long was interrupted
PLUGINS_DIR = Path(__file__).resolve().parent.parent / "plugins"
USER_AGENT = "agency-os prospect search (+https://u9itus.com)"
_KEY = re.compile(r"^[A-Za-z0-9._:-]{1,64}$")


class SearchError(ValueError):
    """A search request that can't be accepted; the message says why."""


class SearchConflict(SearchError):
    """The idempotency_key was already used for a search with different settings."""


class SearchLimit(SearchError):
    """The account's daily cap would be passed (section 5 of docs/U9ITUS_BILLING.md)."""


def daily_cap() -> int:
    try:
        return max(1, int(os.environ.get("AGENCY_OS_ACCOUNT_DAILY_PROSPECTS", DEFAULT_DAILY_PROSPECTS)))
    except ValueError:
        return DEFAULT_DAILY_PROSPECTS


def required_params(kind: str, available: Optional[dict] = None) -> tuple[str, ...]:
    plugin = (available or {}).get(kind)
    if plugin is not None and getattr(plugin, "fields", None) is not None:
        return tuple(f["name"] for f in plugin.fields if f.get("required"))
    return REQUIRED_PARAMS.get(kind, ())


def _canonical(params: dict) -> str:
    return json.dumps(params, sort_keys=True, separators=(",", ":"))


def validate(body: dict, available: Optional[dict] = None) -> tuple[str, dict, int, str]:
    """(type, params, max_results, idempotency_key) from a create request, or SearchError."""
    kind = str(body.get("type") or "")
    known = sorted(available) if available is not None else TYPES
    if kind not in known:
        if kind in TYPES:
            raise SearchError(f"{kind} searches aren't available yet.")
        raise SearchError(f"type must be one of: {', '.join(known)}.")
    params = body.get("params")
    if not isinstance(params, dict):
        raise SearchError("params must be a JSON object.")
    if len(_canonical(params).encode()) > MAX_PARAMS_BYTES:
        raise SearchError(f"params must be at most {MAX_PARAMS_BYTES} bytes.")
    missing = [k for k in required_params(kind, available) if params.get(k) in (None, "", [], {})]
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
                    if callable(getattr(v, "validate", None)) and callable(getattr(v, "run", None))}
    return _plugins


def describe(kind: str, plugin) -> dict:
    """One search type as GET /api/v1/search-types shows it."""
    return {
        "type": kind,
        "label": getattr(plugin, "label", kind),
        "description": getattr(plugin, "description", ""),
        "fields": getattr(plugin, "fields", None)
        or [{"name": n, "label": n, "type": "text", "required": True} for n in REQUIRED_PARAMS.get(kind, ())],
        "attribution": getattr(plugin, "attribution", None),
        "max_results": MAX_RESULTS,
    }


def types(available: Optional[dict] = None) -> list[dict]:
    available = plugins() if available is None else available
    return [describe(kind, available[kind]) for kind in sorted(available)]


def create(db, account_id: int, body: dict, available: Optional[dict] = None) -> tuple[dict, bool]:
    """Queue a search for an account. Returns (search, created); created is False
    when this idempotency_key already made the same search. With `available`
    (search type → plugin), the type must have a plugin and its validate() runs."""
    kind, params, max_results, key = validate(body, available)
    if available is not None:
        plugin = available.get(kind)
        if plugin is None:
            raise SearchError(f"{kind} searches aren't available yet.")
        try:
            params = plugin.validate(params)
        except ValueError as e:
            raise SearchError(str(e)) from None
    c = db.conn
    with c.raw.transaction():
        # One create at a time per account, so two can't both slip under the daily cap.
        c.execute("SELECT pg_advisory_xact_lock(?, ?)", (_CAP_LOCK, account_id))
        existing = c.execute("SELECT * FROM searches WHERE account_id = ? AND idempotency_key = ?",
                             (account_id, key)).fetchone()
        if existing is not None:
            existing = dict(existing)
            if (existing["type"], existing["params"], existing["max_results"]) != (kind, _canonical(params), max_results):
                raise SearchConflict("This idempotency_key was already used for a different search.")
            return existing, False  # a retry is never refused by the cap
        used = committed_today(db, account_id)
        if used + max_results > daily_cap():
            raise SearchLimit(f"This account can get {daily_cap()} prospects a day and has "
                              f"{max(0, daily_cap() - used)} left today. Lower max_results or try tomorrow.")
        row = c.execute(
            """INSERT INTO searches (account_id, idempotency_key, type, params, max_results)
               VALUES (?, ?, ?, ?, ?) RETURNING *""",
            (account_id, key, kind, _canonical(params), max_results),
        ).fetchone()
    return dict(row), True


_CAP_LOCK = 7_402_004  # advisory lock namespace for the daily cap


def committed_today(db, account_id: int) -> int:
    """Prospects delivered today by finished searches, plus max_results of the
    ones still queued or running (they may deliver up to that)."""
    row = db.conn.execute(
        """SELECT COALESCE(SUM(CASE WHEN status IN ('queued', 'running') THEN max_results
                                   ELSE delivered END), 0) AS n
           FROM searches WHERE account_id = ? AND created_at >= CURRENT_DATE""",
        (account_id,),
    ).fetchone()
    return int(row["n"])


def usage(db, start: date, end: date, account_id: Optional[int] = None) -> list[dict]:
    """Per account per day (the day a search was created), start..end inclusive."""
    if end < start:
        raise SearchError("from must be on or before to.")
    if (end - start).days >= MAX_USAGE_DAYS:
        raise SearchError(f"Ask for at most {MAX_USAGE_DAYS} days at a time.")
    where, args = "s.created_at >= ? AND s.created_at < ?", [start, end + timedelta(days=1)]
    if account_id is not None:
        where += " AND s.account_id = ?"
        args.append(account_id)
    rows = db.conn.execute(
        f"""SELECT a.external_ref, CAST(s.created_at AS DATE) AS day, COUNT(*) AS searches,
                   SUM(s.delivered) AS delivered, SUM(s.pages_fetched) AS pages_fetched,
                   SUM(s.api_requests) AS api_requests, SUM(s.cost_cents) AS cost_cents
            FROM searches s JOIN accounts a ON a.id = s.account_id
            WHERE {where}
            GROUP BY a.external_ref, CAST(s.created_at AS DATE)
            ORDER BY day, a.external_ref""",
        args,
    ).fetchall()
    return [{"external_ref": r["external_ref"], "day": r["day"].isoformat(), "searches": int(r["searches"]),
             "delivered": int(r["delivered"]), "pages_fetched": int(r["pages_fetched"]),
             "api_requests": int(r["api_requests"]), "cost_cents": int(r["cost_cents"])} for r in rows]


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
