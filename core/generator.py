"""
Lead package generator: a Super Admin's prospect criteria, run through the
search plugins (plugins/searches/: city, rss, scrape) and the campaign's
enrichers, become a saved list that can be published as a lead package.

A run goes through three steps:

1. find    – each search runs (core/searches.py SearchContext, so plugins work
             unchanged). What they find is saved as a house prospect with an
             outreach row in the chosen campaign. A prospect only a customer
             account found is never used (docs/U9ITUS_BILLING.md decision 8.3),
             and a prospect we already had is linked, not overwritten.
2. enrich  – the chosen enrichers fill in emails and phones
             (Pipeline.enrich_outreach_rows).
3. check   – a saved list (criteria {"generator_run": id}) is made for the
             Super Admin, and selling.preview counts how many leads reach the
             `enriched` tier: an email or phone that hasn't bounced.

Publishing stays a person's decision: the list goes through the normal
/admin/selling publish form, which sets prices and the guarantee.
"""

from __future__ import annotations

import json
import traceback
from typing import Any, Optional

import httpx

from core import searches, selling
from core.db import Database
from core.models import Prospect
from core.registry import PluginRegistry

MAX_LEADS = 500
MAX_SEARCHES = 20
STALE_MINUTES = searches.STALE_MINUTES
GUARANTEE_TIER = "enriched"
FIELD_NAMES = ("name", "website", "address", "city", "state", "zip", "phone", "focus_area")


class GeneratorError(ValueError):
    """Criteria we can't run; the message says why."""


# ── Criteria ───────────────────────────────────────────────────────────


def searches_from_form(form) -> list[dict]:
    """The searches a generator form asks for: one city search per city, one rss
    search per feed, and an optional scrape. Plugins validate them in create()."""
    get = lambda key: str(form.get(key) or "").strip()  # noqa: E731
    found: list[dict] = []
    query, state = get("query"), get("state").upper()
    for city in [c.strip() for c in get("cities").split(",") if c.strip()]:
        found.append({"type": "city", "params": {"city": city, "state": state, "query": query}})
    keywords = [k.strip() for k in get("keywords").split(",") if k.strip()]
    for feed in [f.strip() for f in get("feed_urls").splitlines() if f.strip()]:
        params: dict[str, Any] = {"feed_url": feed}
        if keywords:
            params["keywords"] = keywords
        if get("since"):
            params["since"] = get("since")
        found.append({"type": "rss", "params": params})
    if get("scrape_url"):
        params = {"url": get("scrape_url"), "item_selector": get("item_selector"),
                  "fields": {f: get(f"field_{f}") for f in FIELD_NAMES if get(f"field_{f}")}}
        if get("next_selector"):
            params["next_selector"] = get("next_selector")
        if get("max_pages"):
            try:
                params["max_pages"] = int(get("max_pages"))
            except ValueError:
                raise GeneratorError("Scrape: max pages must be a whole number.") from None
        found.append({"type": "scrape", "params": params})
    return found


def create(db: Database, user, *, title: str, campaign_name: str, searches_wanted: list[dict],
           enrichers: list[str], max_leads: Any, available: Optional[dict] = None,
           known_enrichers: Optional[set[str]] = None) -> dict:
    """Validate the criteria and queue a run. Returns the run, or raises GeneratorError."""
    title = " ".join((title or "").split())[:120]
    if not title:
        raise GeneratorError("Give the run a title, e.g. Austin dentists.")
    campaign_id = db.get_campaign_id(campaign_name or "")
    if not campaign_id:
        raise GeneratorError("Pick the campaign the leads go into.")
    try:
        max_leads = int(max_leads)
    except (TypeError, ValueError):
        max_leads = 0
    if not 1 <= max_leads <= MAX_LEADS:
        raise GeneratorError(f"Max leads must be from 1 to {MAX_LEADS}.")
    if not searches_wanted:
        raise GeneratorError("Add at least one search: cities, a feed or a page to scrape.")
    if len(searches_wanted) > MAX_SEARCHES:
        raise GeneratorError(f"Use at most {MAX_SEARCHES} searches (cities + feeds + scrape) in one run.")
    available = searches.plugins() if available is None else available
    checked = []
    for spec in searches_wanted:
        plugin = available.get(spec.get("type"))
        if plugin is None:
            raise GeneratorError(f"{spec.get('type')} searches aren't available.")
        try:
            checked.append({"type": spec["type"], "params": plugin.validate(dict(spec.get("params") or {}))})
        except ValueError as e:
            raise GeneratorError(f"{spec['type'].capitalize()} search: {e}") from None
    enrichers = [e for e in dict.fromkeys(enrichers or []) if e]
    if known_enrichers is not None:
        unknown = [e for e in enrichers if e not in known_enrichers]
        if unknown:
            raise GeneratorError(f"Unknown enrichers: {', '.join(unknown)}.")
    params = {"searches": checked, "enrichers": enrichers}
    c = db.conn
    with c.raw.transaction():
        run = dict(c.execute(
            """INSERT INTO package_runs (created_by, title, params, campaign_id, max_leads)
               VALUES (?, ?, ?, ?, ?) RETURNING *""",
            (getattr(user, "id", None), title, json.dumps(params), campaign_id, max_leads),
        ).fetchone())
        db._audit(c, user, "generator.run", "package_run", run["id"],
                  {"title": title, "campaign": campaign_name, "max_leads": max_leads,
                   "searches": [s["type"] for s in checked], "enrichers": enrichers})
    return run


# ── Reading and cancelling ─────────────────────────────────────────────


def get(db: Database, run_id: int) -> Optional[dict]:
    row = db.conn.execute(
        """SELECT r.*, c.name AS campaign_name, u.email AS created_by_email FROM package_runs r
           JOIN campaigns c ON c.id = r.campaign_id LEFT JOIN users u ON u.id = r.created_by
           WHERE r.id = ?""", (run_id,)).fetchone()
    if row is None:
        return None
    run = dict(row)
    run["params"] = json.loads(run["params"])
    return run


def recent(db: Database, limit: int = 25) -> list[dict]:
    rows = db.conn.execute(
        """SELECT r.id, r.title, r.status, r.step, r.found, r.enriched, r.eligible, r.max_leads, r.created_at,
                  r.finished_at, c.name AS campaign_name FROM package_runs r JOIN campaigns c ON c.id = r.campaign_id
           ORDER BY r.id DESC LIMIT ?""", (limit,)).fetchall()
    return [dict(r) for r in rows]


def lead_ids(db: Database, run_id: int) -> list[int]:
    return [r["prospect_id"] for r in db.conn.execute(
        "SELECT prospect_id FROM package_run_leads WHERE run_id = ? ORDER BY prospect_id", (run_id,)).fetchall()]


def cancel(db: Database, user, run_id: int) -> Optional[dict]:
    """A queued run ends now; a running one stops at its next check."""
    c = db.conn
    with c.raw.transaction():
        row = c.execute(
            """UPDATE package_runs SET
                 status = CASE WHEN status = 'queued' THEN 'canceled' ELSE status END,
                 finished_at = CASE WHEN status = 'queued' THEN CURRENT_TIMESTAMP ELSE finished_at END,
                 cancel_requested_at = COALESCE(cancel_requested_at, CURRENT_TIMESTAMP)
               WHERE id = ? AND status IN ('queued', 'running') RETURNING id""", (run_id,)).fetchone()
        if row is not None:
            db._audit(c, user, "generator.cancel", "package_run", run_id, {})
    return get(db, run_id)


def reap_stale(db: Database) -> int:
    """Fail runs left running by a restart. What they found is kept."""
    rows = db.conn.execute(
        f"""UPDATE package_runs SET status = 'failed', finished_at = CURRENT_TIMESTAMP,
               error = 'The run was interrupted. Leads found before then are kept.'
            WHERE status = 'running'
              AND COALESCE(heartbeat_at, started_at) < CURRENT_TIMESTAMP - INTERVAL '{STALE_MINUTES} minutes'
            RETURNING id""").fetchall()
    return len(rows)


# ── Running ────────────────────────────────────────────────────────────


def save_lead(db: Database, run: dict, prospect: Prospect) -> bool:
    """Save one found organization into the run. Returns whether it's new to this run."""
    existing = db.find_prospect(prospect)
    if existing is not None and existing["account_id"] is not None:
        return False  # a customer account's prospect: never the house's to use or sell
    c = db.conn
    with c.raw.transaction():
        prospect_id = existing["id"] if existing is not None else db.upsert_prospect(prospect)
        outreach_id = db.upsert_outreach(prospect_id, run["campaign_id"])
        phone = (prospect.metadata or {}).get("phone")
        if phone:
            db.seed_contact(outreach_id, {"phone": phone})
        added = c.execute(
            """INSERT INTO package_run_leads (run_id, prospect_id) VALUES (?, ?)
               ON CONFLICT DO NOTHING RETURNING prospect_id""", (run["id"], prospect_id)).fetchone()
    return added is not None


class Runner:
    """Runs generator runs. Tests pass fake search plugins, a registry and an HTTP transport."""

    def __init__(self, db_url: Optional[str] = None, available: Optional[dict] = None,
                 registry: Optional[PluginRegistry] = None, transport: Optional[httpx.BaseTransport] = None):
        self.db_url = db_url
        self.available = available
        self.registry = registry
        self.transport = transport

    def _registry(self) -> PluginRegistry:
        if self.registry is None:
            self.registry = PluginRegistry()
            self.registry.discover(str(searches.PLUGINS_DIR))
        return self.registry

    @staticmethod
    def _cancelled(db: Database, run_id: int) -> bool:
        row = db.conn.execute("SELECT cancel_requested_at FROM package_runs WHERE id = ?", (run_id,)).fetchone()
        return row is None or row["cancel_requested_at"] is not None

    @staticmethod
    def _update(db: Database, run_id: int, **values) -> None:
        sets = ", ".join(f"{k} = ?" for k in values)
        db.conn.execute(f"UPDATE package_runs SET {sets}, heartbeat_at = CURRENT_TIMESTAMP WHERE id = ?",
                        (*values.values(), run_id))

    def claim(self, db: Database, run_id: Optional[int] = None) -> Optional[dict]:
        """Take a queued run (this one, or the oldest)."""
        where = "id = ?" if run_id is not None else (
            "id = (SELECT id FROM package_runs WHERE status = 'queued' ORDER BY id LIMIT 1 FOR UPDATE SKIP LOCKED)")
        row = db.conn.execute(
            f"""UPDATE package_runs SET status = 'running', step = 'find', started_at = CURRENT_TIMESTAMP,
                       heartbeat_at = CURRENT_TIMESTAMP
                WHERE {where} AND status = 'queued' RETURNING id""",
            (run_id,) if run_id is not None else ()).fetchone()
        return get(db, row["id"]) if row else None

    def _find(self, db: Database, run: dict) -> tuple[int, list[str]]:
        available = self.available if self.available is not None else searches.plugins()
        found, problems = 0, []
        with httpx.Client(timeout=90, transport=self.transport, follow_redirects=True,
                          headers={"User-Agent": searches.USER_AGENT}) as http:
            for spec in run["params"]["searches"]:
                remaining = run["max_leads"] - found
                if remaining <= 0 or self._cancelled(db, run["id"]):
                    break
                plugin = available.get(spec["type"])
                if plugin is None:
                    problems.append(f"{spec['type']}: not available")
                    continue
                ctx = searches.SearchContext({"id": run["id"], "type": spec["type"], "max_results": remaining},
                                             db, http, transport=self.transport,
                                             is_cancelled=lambda: self._cancelled(db, run["id"]))
                try:
                    for prospect in plugin.run(plugin.validate(dict(spec["params"])), ctx):
                        if ctx.cancelled():
                            break
                        if save_lead(db, run, prospect):
                            found += 1
                            ctx.delivered += 1
                            self._update(db, run["id"], found=found)
                        if ctx.remaining == 0:
                            break
                except ValueError as e:  # the plugin's own message
                    problems.append(f"{spec['type']}: {str(e)[:300]}")
                except Exception as e:  # an outage or a bug: log it, keep the other searches going
                    print(f"agency-os: generator run {run['id']}, {spec['type']} search: {type(e).__name__}: {e}\n"
                          f"{traceback.format_exc(limit=5)}")
                    problems.append(f"{spec['type']}: the search failed (details are in the server log)")
                finally:
                    ctx.close()
        return found, problems

    def _enrich(self, db: Database, run: dict) -> int:
        from core.pipeline import Pipeline

        rows = db.conn.execute(
            """SELECT o.id, o.prospect_id FROM package_run_leads l
               JOIN outreach o ON o.prospect_id = l.prospect_id AND o.campaign_id = ?
               WHERE l.run_id = ? AND o.contact_email IS NULL ORDER BY o.id""",
            (run["campaign_id"], run["id"])).fetchall()
        enrichers = run["params"].get("enrichers") or []
        if rows and enrichers:
            Pipeline(db, self._registry()).enrich_outreach_rows(
                rows, enrichers, stop=lambda: self._cancelled(db, run["id"]),
                after_each=lambda n: self._update(db, run["id"], step="enrich"))
        return db.conn.execute(
            """SELECT COUNT(DISTINCT l.prospect_id) FROM package_run_leads l
               JOIN outreach o ON o.prospect_id = l.prospect_id AND o.campaign_id = ?
               WHERE l.run_id = ? AND (o.contact_email IS NOT NULL OR o.contact_phone IS NOT NULL)""",
            (run["campaign_id"], run["id"])).fetchone()[0]

    def _check(self, db: Database, run: dict) -> tuple[Optional[int], int]:
        criteria = {"generator_run": str(run["id"])}
        saved_list_id = None
        if run["created_by"]:
            name = f"{run['title'][:55]} (generated #{run['id']})"
            db.save_prospect_list(run["created_by"], name, criteria)
            saved_list_id = db.conn.execute(
                "SELECT id FROM prospect_saved_lists WHERE user_id = ? AND name = ?",
                (run["created_by"], name)).fetchone()["id"]
        found = selling.preview(db, criteria, GUARANTEE_TIER, selling.hidden_for_publisher(db, run["created_by"]))
        return saved_list_id, len(found["eligible"])

    def run_one(self, db: Database, run: dict) -> dict:
        """Run a claimed run to the end. Never raises; returns the finished run."""
        status, error = "done", None
        try:
            found, problems = self._find(db, run)
            if problems:
                error = "Some searches had problems: " + "; ".join(problems)
            if self._cancelled(db, run["id"]):
                status = "canceled"
            elif not found and problems:
                status = "failed"
            else:
                self._update(db, run["id"], step="enrich")
                enriched = self._enrich(db, run)
                self._update(db, run["id"], step="check", enriched=enriched)
                if self._cancelled(db, run["id"]):
                    status = "canceled"
                else:
                    saved_list_id, eligible = self._check(db, run)
                    self._update(db, run["id"], saved_list_id=saved_list_id, eligible=eligible)
        except Exception as e:  # a bug or an outage: details go to the log
            print(f"agency-os: generator run {run['id']} failed: {type(e).__name__}: {e}\n"
                  f"{traceback.format_exc(limit=5)}")
            status, error = "failed", "The run failed. Leads found before then are kept."
        db.conn.execute(
            """UPDATE package_runs SET status = ?, error = ?, step = NULL, finished_at = CURRENT_TIMESTAMP,
                      heartbeat_at = CURRENT_TIMESTAMP WHERE id = ?""", (status, error, run["id"]))
        return get(db, run["id"])

    def run(self, run_id: Optional[int] = None) -> Optional[dict]:
        """Run one queued run (this one, or the oldest). Returns it finished, or None."""
        db = Database(self.db_url)
        reap_stale(db)
        run = self.claim(db, run_id)
        return self.run_one(db, run) if run else None

    def run_pending(self, limit: int = 10) -> list[dict]:
        finished = []
        while len(finished) < limit:
            run = self.run()
            if run is None:
                break
            finished.append(run)
        return finished
