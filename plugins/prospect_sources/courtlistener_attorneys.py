"""
CourtListener attorneys prospect source: civil litigation attorneys, found as
attorneys of record on recent federal civil cases (PACER dockets mirrored by
the Free Law Project's RECAP archive).

Practice area isn't in state bar records (civil litigation isn't a certified
specialty), so the dockets are the evidence: an attorney appearing on contract,
tort, civil rights or employment cases is a civil litigator. Each prospect
records how many matching cases were found and their nature of suit.

Selling these leads is commercial use, which CourtListener allows only under a
commercial agreement (not a free account or a membership):
https://free.law/membership/allowed-api-usage/. pacer_attorneys gets the same
records from PACER directly.

Needs an API token (courtlistener.com -> Profile -> Developer Tools):
COURTLISTENER_API_TOKEN. The attorney contact details (phone, email, address)
are only served to signed-in API users.

Requests are capped per day (COURTLISTENER_MAX_REQUESTS_PER_DAY, default 100,
under a free account's 125; raise it to what your agreement allows), and each
attorney's details are cached for 30 days, so a run that stops at the cap
picks up where it left off the next day without asking again.

Campaign filters (campaign.yaml `filters`):

    state: CA                       # keep attorneys whose address is in this state
    courts: [cacd, cand, casd, caed]
    suit_natures: [Contract, Torts, Personal Injury, Civil Rights, Labor, Property Rights]
    filed_within_days: 365
    max_dockets: 500                # dockets scanned per run
    min_cases: 1                    # matching cases an attorney needs to be listed
"""

from __future__ import annotations

import os
import time
from datetime import date, timedelta
from typing import Iterator, Optional
from urllib.parse import urlsplit

import httpx

from core.models import Prospect
from core.source_budget import BudgetExhausted, SourceBudget
from plugins.prospect_sources._attorneys import Tally, attorney_prospect, parse_contact  # noqa: F401

BASE = "https://www.courtlistener.com"
SEARCH_URL = f"{BASE}/api/rest/v4/search/"
ATTORNEY_URL = f"{BASE}/api/rest/v4/attorneys/{{id}}/"
DEFAULT_COURTS = ["cacd", "cand", "casd", "caed"]
DEFAULT_SUITS = ["Contract", "Torts", "Personal Injury", "Civil Rights", "Labor", "Property Rights"]
ATTORNEY_CACHE_DAYS = 30


class CourtListenerAttorneysSource:
    """Civil litigation attorneys from federal civil dockets (CourtListener / RECAP)."""

    key = "courtlistener_attorneys"

    def __init__(self, transport: Optional[httpx.BaseTransport] = None, pause: float = 0.75):
        self.transport = transport
        self.pause = pause
        self.db = None

    def bind_db(self, db) -> None:
        """Keep the request count and cache in this database (Pipeline.sync_prospects)."""
        self.db = db

    def _token(self) -> str:
        return os.environ.get("COURTLISTENER_API_TOKEN", "").strip()

    def budget(self) -> SourceBudget:
        try:
            cap = int(os.environ.get("COURTLISTENER_MAX_REQUESTS_PER_DAY", "100"))
        except ValueError:
            cap = 100
        return SourceBudget(self.key, self.db, max_requests_per_day=cap)

    def is_configured(self) -> bool:
        return bool(self._token())

    def discover(self, filters: dict) -> Iterator[Prospect]:
        courts = filters.get("courts") or DEFAULT_COURTS
        suits = filters.get("suit_natures") or DEFAULT_SUITS
        since = date.today() - timedelta(days=int(filters.get("filed_within_days", 365)))
        max_dockets = int(filters.get("max_dockets", 500))
        min_cases = int(filters.get("min_cases", 1))
        state = (filters.get("state") or "").upper()
        budget = self.budget()

        with httpx.Client(timeout=30, transport=self.transport, follow_redirects=False,
                          headers={"Authorization": f"Token {self._token()}",
                                   "User-Agent": "agency-os prospect source"}) as client:
            tallies: dict[int, Tally] = {}
            try:
                for docket in self._dockets(client, budget, courts, suits, since, max_dockets):
                    for attorney_id in set(docket.get("attorney_id") or []):
                        tallies.setdefault(attorney_id, Tally(name="")).add(
                            str(docket.get("docket_id")), docket.get("suitNature"), docket.get("dateFiled"))
            except BudgetExhausted as exc:
                print(f"    ! {exc} (scanned what it could; cached attorneys are still listed)")

            stopped = False
            for attorney_id, tally in sorted(tallies.items(), key=lambda kv: -len(kv[1].cases)):
                if len(tally.cases) < min_cases:
                    break
                details = budget.cached(f"attorney:{int(attorney_id)}", ATTORNEY_CACHE_DAYS)
                if details is None:
                    if stopped:
                        continue  # over the cap: only attorneys already cached go out this run
                    try:
                        details = self._attorney(client, budget, attorney_id)
                    except BudgetExhausted as exc:
                        print(f"    ! {exc}")
                        stopped = True
                        continue
                if not details.get("name"):
                    continue
                prospect = attorney_prospect(
                    self.key, str(attorney_id), f"{BASE}/?type=r&q=attorney_id%3A{int(attorney_id)}",
                    details["name"], details.get("contact_raw") or "", state,
                    email=(details.get("email") or "").strip() or None,
                    phone=(details.get("phone") or "").strip() or None, tally=tally)
                if prospect:
                    yield prospect

    def _get(self, client: httpx.Client, budget: SourceBudget, url: str, params: Optional[dict] = None):
        budget.check()
        resp = client.get(url, params=params)
        budget.record()
        time.sleep(self.pause)
        return resp

    def _dockets(self, client: httpx.Client, budget: SourceBudget, courts: list[str], suits: list[str],
                 since: date, limit: int) -> Iterator[dict]:
        query = "suitNature:(" + " OR ".join(f'"{s}"' for s in suits) + ")"
        url: Optional[str] = SEARCH_URL
        params: Optional[dict] = {"type": "r", "q": query, "court": " ".join(courts),
                                  "filed_after": since.isoformat(), "order_by": "dateFiled desc"}
        seen = 0
        while url and seen < limit:
            resp = self._get(client, budget, url, params)
            resp.raise_for_status()
            body = resp.json()
            for docket in body.get("results") or []:
                seen += 1
                yield docket
                if seen >= limit:
                    return
            url, params = body.get("next"), None
            if url and urlsplit(url).netloc != urlsplit(BASE).netloc:
                return  # never follow a cursor to another host

    def _attorney(self, client: httpx.Client, budget: SourceBudget, attorney_id: int) -> dict:
        """An attorney's name and contact details, fetched once and cached (an empty dict if gone)."""
        resp = self._get(client, budget, ATTORNEY_URL.format(id=int(attorney_id)))
        if resp.status_code == 404:
            details = {}
        else:
            resp.raise_for_status()
            a = resp.json()
            details = {k: a.get(k) for k in ("name", "contact_raw", "email", "phone")}
        budget.store(f"attorney:{int(attorney_id)}", details)
        return details
