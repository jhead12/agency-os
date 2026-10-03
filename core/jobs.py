"""
Background jobs for the deployed dashboard (spec task A7).

On Railway the SQLite database lives on a volume attached to the web service,
and a volume can only be attached to one service, so a separate cron service
can't run the CLI against it. Instead the web app runs these jobs itself: a
loop started in the FastAPI lifespan wakes every few minutes and runs any job
whose interval has passed since its last run (read from the job_runs table,
so a restart or redeploy doesn't re-run a job that just ran).

Off unless AGENCY_OS_RUN_JOBS=1, so local runs and tests never call u9itus.
Owners can also run a job now from /admin/jobs.

Only jobs that are safe to repeat are here: provisioning and event pulls are
idempotent. Sending email (enqueue) stays a deliberate, manual step.
"""

from __future__ import annotations

import asyncio
import json
import os
import threading
import traceback
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable

from core.campaign import CampaignConfig
from core.db import Database
from core.pipeline import Pipeline
from core.registry import PluginRegistry


@dataclass(frozen=True)
class Job:
    key: str
    label: str
    every_minutes: int


def _minutes(env: str, default: int) -> int:
    try:
        return max(5, int(os.environ.get(env, default)))
    except ValueError:
        return default


def configured_jobs() -> list[Job]:
    return [
        Job("pull-events", "Pull u9itus portal events (views, claims, publishes)",
            _minutes("AGENCY_OS_PULL_EVENTS_MINUTES", 60)),
        Job("provision", "Create demo pages for new prospects",
            _minutes("AGENCY_OS_PROVISION_MINUTES", 24 * 60)),
    ]


def jobs_enabled() -> bool:
    return os.environ.get("AGENCY_OS_RUN_JOBS", "").strip().lower() in ("1", "true", "yes", "on")


class JobRunner:
    """Runs jobs against one database. One run of a given job at a time."""

    def __init__(self, db_path: str, plugins_dir: str, load_campaigns: Callable[[], list[CampaignConfig]]):
        self.db_path = db_path
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

        db = Database(self.db_path)
        run_id = db.conn.execute(
            "INSERT INTO job_runs (job, trigger, started_at) VALUES (?, ?, ?)",
            (key, trigger, datetime.now().isoformat(timespec="seconds")),
        ).lastrowid
        db.conn.commit()
        try:
            summary = self._execute(key, db)
            # A product that isn't configured did nothing; show that as a problem.
            ok = not any(("error" in stats or stats.get("api_not_configured"))
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
        db.conn.commit()
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
                product = registry.get_product(campaign.product)
                if product and hasattr(product, "pull_events"):
                    by_product.setdefault(campaign.product, campaign)
            return {product: pipeline.pull_product_events(campaign)
                    for product, campaign in by_product.items()}

        if key == "provision":
            return {campaign.db_name: pipeline.provision_demos(campaign)
                    for campaign in campaigns
                    if hasattr(registry.get_product(campaign.product), "provision_demo")}

        raise ValueError(f"Unknown job {key!r}")

    @staticmethod
    def _campaign_active(db: Database, campaign: CampaignConfig) -> bool:
        row = db.conn.execute("SELECT is_active FROM campaigns WHERE name = ?", (campaign.db_name,)).fetchone()
        return row is not None and bool(row["is_active"])

    # ── Schedule ───────────────────────────────────────────────────────

    def last_runs(self) -> dict[str, dict]:
        db = Database(self.db_path)
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
        db = Database(self.db_path)
        rows = db.conn.execute("SELECT * FROM job_runs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(row) for row in rows]

    async def loop(self, check_every_seconds: int = 300, first_delay_seconds: int = 60) -> None:
        """Run due jobs forever. Each job runs in a worker thread so requests aren't blocked."""
        await asyncio.sleep(first_delay_seconds)
        while True:
            for job in self.due_jobs():
                await asyncio.to_thread(self.run, job.key, "schedule")
            await asyncio.sleep(check_every_seconds)
