"""
PACER attorneys prospect source: civil litigation attorneys, found as attorneys
of record on recent federal civil cases, straight from the courts' own PACER
system (court records are public, with no resale restriction).

1. The PACER Case Locator (PCL) API finds civil cases by court, nature of suit
   and filing date ($0.10 a search page of 54 cases).
   https://pacer.uscourts.gov/help/pacer/pacer-case-locator-pcl-api-user-guide
2. Each case's docket report is fetched with only its parties and attorneys
   (no docket entries), which keeps it to a page or two: $0.10 per 4,320
   bytes, never more than $3.00 a docket.
   Docket reports are fetched and parsed by juriscraper (Free Law Project's
   open-source PACER library, BSD-2): pip install -r requirements-pacer.txt

Needs a PACER account: PACER_USERNAME, PACER_PASSWORD (and PACER_CLIENT_CODE
if your account uses one).

Spending is capped per calendar month (PACER_MAX_USD_PER_MONTH, default $10):
PACER waives a quarter's fees when they total $30 or less, so the default keeps
a run of three months free. A request is only made when the cap leaves room
for its highest possible cost, so the cap is never exceeded. Search pages are
cached for a day and each case's attorney list for 90 days, so re-running a
sync doesn't pay for the same records twice.

Campaign filters (campaign.yaml `filters`):

    state: CA                          # keep attorneys whose address is in this state
    pacer_courts: [cac, can, cas, cae] # PCL court ids (California's four districts)
    nature_of_suit: ["190", "360", ...]  # PCL codes; default: CIVIL_LITIGATION below
    filed_within_days: 90
    max_cases: 100                     # cases read per run
    min_cases: 1
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
from datetime import date, timedelta
from typing import Callable, Iterator, Optional

import httpx

from core.models import Prospect
from core.source_budget import BudgetExhausted, SourceBudget
from plugins.prospect_sources._attorneys import EMAIL_RE, Tally, attorney_prospect

PCL_URL = "https://pcl.uscourts.gov/pcl-public-api/rest/cases/find"
SEARCH_PAGE_CENTS = 10
DOCKET_MAX_CENTS = 300
BYTES_PER_PAGE = 4320
SEARCH_CACHE_DAYS = 1
CASE_CACHE_DAYS = 90
DEFAULT_COURTS = ["cac", "can", "cas", "cae"]

# Civil litigation natures of suit (PCL API user guide, Appendix C): contract,
# real property, torts / personal injury, civil rights, labor, consumer and
# business disputes. Leaves out prisoner, immigration, social security, tax,
# forfeiture and bankruptcy matters, which other kinds of lawyers handle.
CIVIL_LITIGATION = {
    "110": "Insurance", "140": "Negotiable Instrument", "150": "Contract: Recovery/Enforcement",
    "160": "Stockholders Suits", "190": "Contract: Other", "195": "Contract Product Liability",
    "196": "Contract: Franchise", "220": "Real Property: Foreclosure", "230": "Rent Lease & Ejectment",
    "240": "Torts to Land", "245": "Tort Product Liability", "290": "Real Property: Other",
    "320": "Assault Libel & Slander", "350": "Motor Vehicle", "355": "Motor Vehicle Product Liability",
    "360": "P.I.: Other", "362": "Personal Injury Medical Malpractice", "365": "Personal Injury Product Liability",
    "367": "Personal Injury: Health Care/Pharmaceutical", "370": "Fraud or Truth-In-Lending",
    "380": "Personal Property: Other", "385": "Prop. Damage Prod. Liability", "410": "Anti-Trust",
    "440": "Civil Rights: Other", "442": "Civil Rights: Jobs", "443": "Civil Rights: Accomodations",
    "445": "Civil Rights: ADA - Employment", "446": "Civil Rights: ADA - Other", "470": "Racketeer/Corrupt Organization",
    "480": "Consumer Credit", "710": "Labor: Fair Standards", "720": "Labor: Labor/Management Relations",
    "751": "Labor: Family and Medical Leave Act", "790": "Labor: Other", "791": "Labor: E.R.I.S.A.",
    "820": "Copyright", "830": "Patent", "840": "Trademark", "850": "Securities/Commodities",
}

# (session, ecf court id, pacer case id) -> (attorneys [{"name", "contact"}], bytes billed)
DocketFetcher = Callable[[object, str, str], tuple[list[dict], int]]


def ecf_court(pcl_court: str) -> str:
    """PCL result court ids name the court type ("cacdc"); CM/ECF hosts drop the last letter ("cacd")."""
    court = (pcl_court or "").lower()
    return court[:-1] if court.endswith("dc") else court


def docket_cents(nbytes: int) -> int:
    """PACER's fee for an HTML report: $0.10 per 4,320 bytes, at least one page, capped at 30 pages."""
    return min(30, max(1, math.ceil(nbytes / BYTES_PER_PAGE))) * 10


def attorney_key(name: str, contact: str) -> str:
    """The same attorney across cases: their name plus their email (or ZIP), since PACER has no attorney id."""
    email = EMAIL_RE.search(contact or "")
    zip_code = re.search(r"\b\d{5}\b", contact or "")
    anchor = email[0].lower() if email else (zip_code[0] if zip_code else "")
    raw = f"{' '.join(name.lower().split())}|{anchor}"
    return "pacer:" + hashlib.sha1(raw.encode()).hexdigest()[:16]


def juriscraper_docket(session, court_id: str, case_id: str) -> tuple[list[dict], int]:
    """A case's attorneys from its CM/ECF docket report, without docket entries."""
    from juriscraper.pacer import DocketReport

    report = DocketReport(court_id, session)
    tomorrow = date.today() + timedelta(days=1)
    # A date range in the future leaves out every docket entry: only the
    # header, parties and counsel are returned (and billed).
    report.query(case_id, show_parties_and_counsel=True, date_range_type="Filed",
                 date_start=tomorrow, date_end=tomorrow, include_pdf_headers=False)
    attorneys = [{"name": a.get("name", ""), "contact": a.get("contact", "")}
                 for party in report.parties for a in party.get("attorneys", [])]
    return attorneys, len(report.response.content) if report.response is not None else 0


class PacerAttorneysSource:
    """Civil litigation attorneys from PACER (Case Locator + docket reports)."""

    key = "pacer_attorneys"

    def __init__(self, transport: Optional[httpx.BaseTransport] = None,
                 login: Optional[Callable[[], tuple[str, object]]] = None,
                 docket_fetcher: DocketFetcher = juriscraper_docket):
        self.transport = transport
        self._login = login or self._juriscraper_login
        self.docket_fetcher = docket_fetcher
        self.db = None

    def bind_db(self, db) -> None:
        """Keep spending and the cache in this database (Pipeline.sync_prospects)."""
        self.db = db

    def is_configured(self) -> bool:
        if not (os.environ.get("PACER_USERNAME") and os.environ.get("PACER_PASSWORD")):
            return False
        try:
            import juriscraper.pacer  # noqa: F401
        except ImportError:
            print("    ! pacer_attorneys needs juriscraper: pip install -r requirements-pacer.txt")
            return False
        return True

    def budget(self) -> SourceBudget:
        try:
            dollars = float(os.environ.get("PACER_MAX_USD_PER_MONTH", "10"))
        except ValueError:
            dollars = 10.0
        return SourceBudget(self.key, self.db, max_cents_per_month=max(0, round(dollars * 100)))

    def _juriscraper_login(self) -> tuple[str, object]:
        from juriscraper.pacer import PacerSession

        session = PacerSession(username=os.environ["PACER_USERNAME"], password=os.environ["PACER_PASSWORD"],
                               client_code=os.environ.get("PACER_CLIENT_CODE") or None)
        session.login()
        return session.cookies.get("NextGenCSO"), session

    def discover(self, filters: dict) -> Iterator[Prospect]:
        criteria = {
            "jurisdictionType": "cv",
            "courtId": filters.get("pacer_courts") or DEFAULT_COURTS,
            "natureOfSuit": [str(c) for c in filters.get("nature_of_suit") or CIVIL_LITIGATION],
            "dateFiledFrom": (date.today() - timedelta(days=int(filters.get("filed_within_days", 90)))).isoformat(),
            "dateFiledTo": date.today().isoformat(),
        }
        max_cases = int(filters.get("max_cases", 100))
        min_cases = int(filters.get("min_cases", 1))
        state = (filters.get("state") or "").upper()
        budget = self.budget()
        token, session = None, None

        def logged_in():
            nonlocal token, session
            if token is None:
                token, session = self._login()
            return token, session

        tallies: dict[str, Tally] = {}
        with httpx.Client(timeout=60, transport=self.transport,
                          headers={"Accept": "application/json", "User-Agent": "agency-os prospect source"}) as client:
            try:
                for case in self._cases(client, budget, criteria, max_cases, logged_in):
                    attorneys = self._attorneys(budget, case, logged_in)
                    case_ref = f"{case.get('courtId')}:{case.get('caseId')}"
                    nature = CIVIL_LITIGATION.get(str(case.get("natureOfSuit")), str(case.get("natureOfSuit") or ""))
                    for a in attorneys:
                        tally = tallies.setdefault(attorney_key(a["name"], a["contact"]),
                                                   Tally(name=a["name"], contact_raw=a["contact"]))
                        tally.add(case_ref, nature, case.get("dateFiled"))
            except BudgetExhausted as exc:
                print(f"    ! {exc} (listing the attorneys found so far)")

        for ref, tally in sorted(tallies.items(), key=lambda kv: -len(kv[1].cases)):
            if len(tally.cases) < min_cases:
                break
            prospect = attorney_prospect(self.key, ref, "https://pcl.uscourts.gov", tally.name,
                                         tally.contact_raw, state, tally=tally)
            if prospect:
                yield prospect

    def _cases(self, client: httpx.Client, budget: SourceBudget, criteria: dict, limit: int,
               logged_in) -> Iterator[dict]:
        seen, page = 0, 0
        while seen < limit:
            key = "search:" + hashlib.sha1(json.dumps([criteria, page], sort_keys=True).encode()).hexdigest()
            body = budget.cached(key, SEARCH_CACHE_DAYS)
            if body is None:
                budget.check(cents=SEARCH_PAGE_CENTS)
                token, _ = logged_in()
                resp = client.post(PCL_URL, params={"page": page}, json=criteria,
                                   headers={"X-NEXT-GEN-CSO": token})
                resp.raise_for_status()
                body = resp.json()
                fee = (body.get("receipt") or {}).get("searchFee")
                budget.record(cents=round(float(fee) * 100) if fee else SEARCH_PAGE_CENTS)
                budget.store(key, body)
            for case in body.get("content") or []:
                seen += 1
                yield case
                if seen >= limit:
                    return
            if (body.get("pageInfo") or {}).get("last", True):
                return
            page += 1

    def _attorneys(self, budget: SourceBudget, case: dict, logged_in) -> list[dict]:
        court, case_id = ecf_court(case.get("courtId")), str(case.get("caseId") or "")
        if not court or not case_id:
            return []
        key = f"case:{court}:{case_id}"
        cached = budget.cached(key, CASE_CACHE_DAYS)
        if cached is not None:
            return cached
        budget.check(cents=DOCKET_MAX_CENTS)  # room for the most a docket can cost
        _, session = logged_in()
        attorneys, nbytes = self.docket_fetcher(session, court, case_id)
        budget.record(cents=docket_cents(nbytes))
        budget.store(key, attorneys)
        return attorneys
