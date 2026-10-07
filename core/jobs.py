"""
Background jobs for the deployed dashboard (spec task A7).

On Railway the SQLite database lives on a volume attached to the web service,
and a volume can only be attached to one service, so a separate cron service
can't run the CLI against it. Instead the web app runs these jobs itself: a
loop started in the FastAPI lifespan wakes every few minutes and runs any job
whose interval has passed since its last run (read from the job_runs table,
so a restart or redeploy doesn't re-run a job that just ran).

Off unless AGENCY_OS_RUN_JOBS=1, so local runs and tests never call a product's API.
Owners can also run a job now from /admin/jobs.

Only jobs that are safe to repeat are here: provisioning and event pulls are
idempotent. Sending email (enqueue) stays a deliberate, manual step.

Plugins add jobs in plugins/jobs/<name>.py, found once per process (restart to
add one):

    class StaleLeadsJob:
        key = "stale-leads"            # lowercase; can't reuse a built-in job's key
        label = "Rank stale leads with an agent"
        every_minutes = 24 * 60        # at least 5; AGENCY_OS_JOB_STALE_LEADS_MINUTES overrides

        def run(self, job: JobContext) -> dict:
            return {"ranked": 12}      # the run's summary, shown on /admin/jobs

The same rules hold: a plugin job must be safe to repeat and must never send
anything (email, texts, mail) or spend money. job.ask_agent() drafts with a
persona and returns text; a job stores what it found in its summary, and a
plugin page shows it with last_result(). Jobs see every campaign, so a job
keeps results per campaign and its page shows only the campaigns the viewer sees.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import threading
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Optional

from core import agents
from core.campaign import CampaignConfig
from core.db import Database
from core.pipeline import Pipeline
from core.protocols import portal_product
from core.registry import PluginRegistry


PLUGINS_DIR = Path(__file__).resolve().parent.parent / "plugins"
BUILT_IN = ("pull-events", "provision")
KEY_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
MIN_MINUTES = 5


@dataclass(frozen=True)
class Job:
    key: str
    label: str
    every_minutes: int
    plugin: Any = field(default=None, compare=False, repr=False)  # a plugins/jobs/ instance


@dataclass
class JobContext:
    """What a plugin job gets for one run."""
    key: str
    db: Database
    registry: PluginRegistry
    pipeline: Pipeline
    campaigns: list[CampaignConfig]  # active campaigns, all of them (jobs run as no one)

    def ask_agent(self, persona_key: str, ask: str, data) -> dict:
        """Ask a persona (agents/ or plugins/agents/) about `data`. Returns {ok, text, error, agent}; never sends."""
        return agents.ask(persona_key, ask, data)

    def last_result(self) -> Optional[dict]:
        """This job's previous run (see last_result()), e.g. to pick up where it left off."""
        return last_result(self.db, self.key)


def _minutes(env: str, default: int) -> int:
    try:
        return max(5, int(os.environ.get(env, default)))
    except ValueError:
        return default


_plugin_jobs: list[Job] | None = None


def plugin_jobs() -> list[Job]:
    """The valid jobs in plugins/jobs/, found once per process."""
    global _plugin_jobs
    if _plugin_jobs is None:
        registry = PluginRegistry()
        registry.discover(str(PLUGINS_DIR), categories=("jobs",))
        _plugin_jobs = []
        for key, plugin in registry.jobs.items():
            label, every = getattr(plugin, "label", None), getattr(plugin, "every_minutes", None)
            if (not KEY_RE.match(key) or key in BUILT_IN or not isinstance(label, str) or not label
                    or not isinstance(every, int) or isinstance(every, bool) or not callable(getattr(plugin, "run", None))):
                print(f"  ! jobs/{key}: needs a lowercase key (not {' or '.join(BUILT_IN)}), a label, "
                      "every_minutes and run()")
                continue
            env = "AGENCY_OS_JOB_" + re.sub(r"[^A-Z0-9]", "_", key.upper()) + "_MINUTES"
            _plugin_jobs.append(Job(key, label, _minutes(env, max(MIN_MINUTES, every)), plugin))
    return _plugin_jobs


def configured_jobs() -> list[Job]:
    return [
        Job("pull-events", "Pull demo portal events (views, claims, publishes)",
            _minutes("AGENCY_OS_PULL_EVENTS_MINUTES", 60)),
        Job("provision", "Create demo pages for new prospects",
            _minutes("AGENCY_OS_PROVISION_MINUTES", 24 * 60)),
        *plugin_jobs(),
    ]


def last_result(db: Database, key: str) -> Optional[dict]:
    """A job's latest finished run: {started_at, finished_at, ok, trigger, summary (a dict)}, or None."""
    row = db.conn.execute(
        "SELECT * FROM job_runs WHERE job = ? AND finished_at IS NOT NULL ORDER BY id DESC LIMIT 1", (key,)
    ).fetchone()
    if row is None:
        return None
    found = dict(row)
    try:
        found["summary"] = json.loads(found.get("summary") or "{}")
    except ValueError:
        found["summary"] = {}
    return found


def jobs_enabled() -> bool:
    return os.environ.get("AGENCY_OS_RUN_JOBS", "").strip().lower() in ("1", "true", "yes", "on")


class JobRunner:
    """Runs jobs against one database. One run of a given job at a time."""

    def __init__(self, db_url: str, plugins_dir: str, load_campaigns: Callable[[], list[CampaignConfig]]):
        self.db_url = db_url
        self.plugins_dir = plugins_dir
        self.load_campaigns = load_campaigns
        self._locks = {job.key: threading.Lock() for job in configured_jobs()}

    # ── Running ────────────────────────────────────────────────────────

    def run(self, key: str, trigger: str = "schedule") -> dict:
        """Run one job now. Returns {"ok", "summary"}; never raises."""
        lock = self._locks.get(key)
        if lock is None:
            return {"ok": False, "summary": {"error": f"Unknown job {key!r}"}}
        if not lock.acquire(blocking=False):
            return {"ok": False, "summary": {"error": "Already running"}}

        db = Database(self.db_url)
        run_id = db.conn.execute(
            "INSERT INTO job_runs (job, trigger, started_at) VALUES (?, ?, ?) RETURNING id",
            (key, trigger, datetime.now().isoformat(timespec="seconds")),
        ).fetchone()["id"]
        try:
            summary = self._execute(key, db)
            # A product that isn't configured did nothing; show that as a problem.
            ok = "error" not in summary and not any(("error" in stats or stats.get("api_not_configured"))
                                                    for stats in summary.values() if isinstance(stats, dict))
        except Exception as exc:  # a job must never take the web app down
            summary = {"error": f"{type(exc).__name__}: {exc}", "trace": traceback.format_exc(limit=5)}
            ok = False
        finally:
            lock.release()

        db.conn.execute(
            "UPDATE job_runs SET finished_at = ?, ok = ?, summary = ? WHERE id = ?",
            (datetime.now().isoformat(timespec="seconds"), int(ok), json.dumps(summary, default=str), run_id),
        )
        return {"ok": ok, "summary": summary}

    def _execute(self, key: str, db: Database) -> dict:
        registry = PluginRegistry()
        registry.discover(self.plugins_dir)
        pipeline = Pipeline(db, registry)
        campaigns = [c for c in self.load_campaigns() if self._campaign_active(db, c)]

        if key == "pull-events":
            # The event cursor is per product, and an event applies to the prospect
            # in every campaign, so pull once per product.
            by_product: dict[str, CampaignConfig] = {}
            for campaign in campaigns:
                if portal_product(registry.get_product(campaign.product)):
                    by_product.setdefault(campaign.product, campaign)
            return {product: pipeline.pull_product_events(campaign)
                    for product, campaign in by_product.items()}

        if key == "provision":
            return {campaign.db_name: pipeline.provision_demos(campaign)
                    for campaign in campaigns
                    if portal_product(registry.get_product(campaign.product))}

        job = next((j for j in plugin_jobs() if j.key == key), None)
        if job is None:
            raise ValueError(f"Unknown job {key!r}")
        summary = job.plugin.run(JobContext(key, db, registry, pipeline, campaigns))
        return summary if isinstance(summary, dict) else {"result": summary}

    @staticmethod
    def _campaign_active(db: Database, campaign: CampaignConfig) -> bool:
        row = db.conn.execute("SELECT is_active FROM campaigns WHERE name = ?", (campaign.db_name,)).fetchone()
        return row is not None and bool(row["is_active"])

    # ── Schedule ───────────────────────────────────────────────────────

    def last_runs(self) -> dict[str, dict]:
        db = Database(self.db_url)
        rows = db.conn.execute(
            """SELECT r.* FROM job_runs r
               JOIN (SELECT job, MAX(id) AS id FROM job_runs GROUP BY job) latest ON latest.id = r.id"""
        ).fetchall()
        return {row["job"]: dict(row) for row in rows}

    def due_jobs(self, now: datetime | None = None) -> list[Job]:
        now = now or datetime.now()
        last = self.last_runs()
        due = []
        for job in configured_jobs():
            started = last.get(job.key, {}).get("started_at")
            if not started or datetime.fromisoformat(started) <= now - timedelta(minutes=job.every_minutes):
                due.append(job)
        return due

    def recent_runs(self, limit: int = 30) -> list[dict]:
        db = Database(self.db_url)
        rows = db.conn.execute("SELECT * FROM job_runs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(row) for row in rows]

    async def loop(self, check_every_seconds: int = 300, first_delay_seconds: int = 60) -> None:
        """Run due jobs forever. Each job runs in a worker thread so requests aren't blocked."""
        await asyncio.sleep(first_delay_seconds)
        while True:
            for job in self.due_jobs():
                await asyncio.to_thread(self.run, job.key, "schedule")
            await asyncio.sleep(check_every_seconds)
