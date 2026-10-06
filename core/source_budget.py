"""
Limits and a cache for prospect sources that cost money or are rate limited
(CourtListener's API allowance, PACER's per-page fees).

- A request is checked against the limits *before* it is made, so a limit is
  never exceeded: a run that reaches one stops with what it has, and the
  next run (tomorrow, or next month) carries on.
- Usage is kept per source per day in source_usage, so the limits hold across
  runs, the CLI and the scheduled jobs.
- Lookups worth keeping (an attorney's contact details, a case's attorney
  list) are cached in source_cache, so re-running a sync doesn't pay for them
  again.

Without a database (a source used on its own, or in tests) the counts and the
cache live in memory for the run.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from typing import Any, Optional


class BudgetExhausted(RuntimeError):
    """A request would go over a source's limit; the message says which and until when."""


class SourceBudget:
    def __init__(self, source: str, db=None, *, max_requests_per_day: int = 0, max_cents_per_month: int = 0,
                 today: Optional[date] = None):
        """Limits of 0 mean no limit."""
        self.source = source
        self.db = db
        self.max_requests_per_day = max(0, int(max_requests_per_day or 0))
        self.max_cents_per_month = max(0, int(max_cents_per_month or 0))
        self.today = today or date.today()
        self._usage: dict[date, list[int]] = {}
        self._cache: dict[str, tuple[datetime, Any]] = {}

    # ── Usage ──────────────────────────────────────────────────────────

    def requests_today(self) -> int:
        if self.db is None:
            return self._usage.get(self.today, [0, 0])[0]
        row = self.db.conn.execute("SELECT requests FROM source_usage WHERE source = ? AND day = ?",
                                   (self.source, self.today)).fetchone()
        return row["requests"] if row else 0

    def cents_this_month(self) -> int:
        month = self.today.replace(day=1)
        if self.db is None:
            return sum(cents for day, (_, cents) in self._usage.items() if day >= month)
        return self.db.conn.execute(
            "SELECT COALESCE(SUM(cost_cents), 0) FROM source_usage WHERE source = ? AND day >= ?",
            (self.source, month)).fetchone()[0]

    def check(self, requests: int = 1, cents: int = 0) -> None:
        """Raise BudgetExhausted if `requests` more requests costing up to `cents` would go over a limit."""
        if self.max_requests_per_day and self.requests_today() + requests > self.max_requests_per_day:
            raise BudgetExhausted(f"{self.source}: reached the limit of {self.max_requests_per_day} "
                                  f"requests today; the next run tomorrow continues")
        if self.max_cents_per_month and self.cents_this_month() + cents > self.max_cents_per_month:
            raise BudgetExhausted(f"{self.source}: reached the limit of ${self.max_cents_per_month / 100:.2f} "
                                  f"this month (${self.cents_this_month() / 100:.2f} spent)")

    def record(self, requests: int = 1, cents: int = 0) -> None:
        if self.db is None:
            used = self._usage.setdefault(self.today, [0, 0])
            used[0] += requests
            used[1] += cents
            return
        self.db.conn.execute(
            """INSERT INTO source_usage (source, day, requests, cost_cents) VALUES (?, ?, ?, ?)
               ON CONFLICT (source, day) DO UPDATE SET requests = source_usage.requests + EXCLUDED.requests,
                                                       cost_cents = source_usage.cost_cents + EXCLUDED.cost_cents""",
            (self.source, self.today, requests, cents))

    # ── Cache ──────────────────────────────────────────────────────────

    def cached(self, key: str, max_age_days: int) -> Optional[Any]:
        """The value stored under `key` within the last `max_age_days`, else None."""
        if self.db is None:
            hit = self._cache.get(key)
            return hit[1] if hit and hit[0] > datetime.now() - timedelta(days=max_age_days) else None
        row = self.db.conn.execute(
            """SELECT value FROM source_cache WHERE source = ? AND key = ?
                 AND fetched_at > CURRENT_TIMESTAMP - (? * INTERVAL '1 day')""",
            (self.source, key, max_age_days)).fetchone()
        return json.loads(row["value"]) if row else None

    def store(self, key: str, value: Any) -> None:
        if self.db is None:
            self._cache[key] = (datetime.now(), value)
            return
        self.db.conn.execute(
            """INSERT INTO source_cache (source, key, value) VALUES (?, ?, ?)
               ON CONFLICT (source, key) DO UPDATE SET value = EXCLUDED.value, fetched_at = CURRENT_TIMESTAMP""",
            (self.source, key, json.dumps(value)))
