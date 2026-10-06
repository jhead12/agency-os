"""
Recruiting campaigns (campaign.yaml `requires_permission: recruiting.view`):
their leads are visible only to recruiters and owners, everywhere — lists,
detail pages, the call log, AI tools — and recruiters buy lead packages into
them like any other campaign. Also the sources that feed them (NPI Registry,
CourtListener attorneys) and the platform's cut of package sales.

Run: TEST_DATABASE_URL=postgresql://localhost/agency_os_test python -m pytest tests/test_recruiting.py
"""

import json
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import access, lead_packages, selling, tools  # noqa: E402
from core.models import CallLog, Prospect  # noqa: E402
from plugins.prospect_sources.courtlistener_attorneys import CourtListenerAttorneysSource  # noqa: E402
from plugins.prospect_sources.npi_registry import NpiRegistrySource, to_prospect  # noqa: E402
import web.app as webapp  # noqa: E402
from tests.test_access import client_for, db, make_user  # noqa: E402,F401

RECRUITING = "recruiting--civil-litigation-attorneys-ca"
HEALTHCARE = "healthcare-practices--dentists--chiropractors-pasadena-area"


def campaign_names():
    return {c.db_name for c in webapp.get_campaigns()}


@pytest.fixture
def leads(db):
    """One attorney in the recruiting campaign, one dentist in the healthcare campaign."""
    assert {RECRUITING, HEALTHCARE} <= campaign_names()
    attorney = db.upsert_prospect(Prospect(name="Avery Attorney", state="CA", city="Oakland", source="t"))
    dentist = db.upsert_prospect(Prospect(name="Bright Smile Dental", state="CA", city="Pasadena", source="t"))
    attorney_outreach = db.upsert_outreach(attorney, db.upsert_campaign(RECRUITING, "x"))
    dentist_outreach = db.upsert_outreach(dentist, db.upsert_campaign(HEALTHCARE, "x"))
    db.log_call(CallLog(outreach_id=attorney_outreach, campaign_id=db.get_campaign_id(RECRUITING),
                        prospect_id=attorney, outcome="completed", notes="Avery call"))
    return {"attorney": attorney, "dentist": dentist, "dentist_outreach": dentist_outreach}


def test_recruiter_role_buys_and_sees_recruiting_sales_rep_does_not():
    recruiter = set(access.STARTER_ROLES["Recruiter"][1])
    assert {"recruiting.view", "packages.view", "packages.buy", "spend.view"} <= recruiter
    assert set(access.CATALOG) >= recruiter
    assert "recruiting.view" not in access.STARTER_ROLES["Sales Rep"][1]


def test_attorney_leads_are_hidden_from_sales_reps_everywhere(db, leads):
    make_user(db, "rep@x.com", "Sales Rep")
    rep = client_for("rep@x.com")
    page = rep.get("/prospects").text
    assert "Bright Smile Dental" in page and "Avery Attorney" not in page
    assert rep.get(f"/prospects/{leads['attorney']}").status_code == 404
    assert rep.post(f"/prospects/{leads['attorney']}/stage", data={"stage": "engaged"},
                    headers={"origin": "http://testserver"}).status_code == 404
    assert rep.get(f"/prospects/{leads['dentist']}").status_code == 200
    assert "Avery" not in rep.get("/call-log").text
    assert "Civil Litigation" not in rep.get("/campaigns").text
    assert RECRUITING not in {s.get("campaign") for s in rep.get("/api/stats").json()}
    assert "Avery" not in rep.get(f"/call-scripts?prospect_id={leads['dentist']}").text
    assert rep.get(f"/call-scripts?prospect_id={leads['attorney']}").status_code == 404


def test_recruiters_and_owners_see_attorney_leads(db, leads):
    make_user(db, "recruiter@x.com", "Recruiter")
    make_user(db, "owner@x.com", access.OWNER_ROLE)
    for email in ("recruiter@x.com", "owner@x.com"):
        client = client_for(email)
        assert "Avery Attorney" in client.get("/prospects").text
        assert client.get(f"/prospects/{leads['attorney']}").status_code == 200
        assert "Avery" in client.get("/call-log").text


def test_ai_tools_follow_the_same_rule(db, leads):
    rep = db.load_current_user(make_user(db, "rep@x.com", "Sales Rep"))
    recruiter = db.load_current_user(make_user(db, "recruiter@x.com", "Recruiter"))
    found = tools.run_tool(db, rep, "search_prospects", {}, source="test")["prospects"]
    assert {p["name"] for p in found} == {"Bright Smile Dental"}
    assert tools.run_tool(db, rep, "get_prospect", {"prospect_id": leads["attorney"]}, source="test") == {
        "ok": False, "error": "Prospect not found"}
    assert tools.run_tool(db, rep, "get_campaign", {"campaign": RECRUITING}, source="test")["ok"] is False
    assert tools.run_tool(db, recruiter, "get_prospect", {"prospect_id": leads["attorney"]},
                          source="test")["prospect"]["name"] == "Avery Attorney"


def test_only_recruiters_can_buy_packages_into_the_recruiting_campaign(db, monkeypatch):
    monkeypatch.setenv("AGENCY_OS_LEAD_PROVIDERS", "https://provider.example")
    monkeypatch.setattr(lead_packages, "find_package", lambda provider, package_id: (None, "reached the provider"))
    db.create_role("Buyer", "Buys lead packages, not a recruiter", ["packages.view", "packages.buy"], actor=None)
    make_user(db, "buyer@x.com", "Buyer")
    make_user(db, "recruiter@x.com", "Recruiter")
    url = f"/lead-packages/review?provider=https://provider.example&package_id=p1&campaign={RECRUITING}"

    def error(client):
        return parse_qs(urlsplit(client.get(url).headers["location"]).query)["error"][0]

    assert error(client_for("buyer@x.com")) == "Pick a campaign that has lead packages turned on."
    assert error(client_for("recruiter@x.com")) == "reached the provider"


def test_source_ids_dedupe_and_seeded_contacts_never_overwrite(db):
    first = db.upsert_prospect(Prospect(name="Smile Dental", state="CA", source="npi_registry",
                                        metadata={"external_ref": "111"}))
    other = db.upsert_prospect(Prospect(name="Smile Dental", state="CA", source="npi_registry",
                                        metadata={"external_ref": "222"}))
    again = db.upsert_prospect(Prospect(name="Smile Dental Group", state="CA", source="npi_registry",
                                        metadata={"external_ref": "111"}))
    assert first != other and again == first
    outreach_id = db.upsert_outreach(first, db.upsert_campaign(HEALTHCARE, "x"))
    db.update_outreach(outreach_id, {"contact_phone": "(626) 555-0100"})
    db.seed_contact(outreach_id, {"name": "Dr. Lee", "phone": "(626) 555-9999", "title": "Owner"})
    row = db.get_outreach(outreach_id)
    assert (row.contact_name, row.contact_phone, row.contact_title) == ("Dr. Lee", "(626) 555-0100", "Owner")


# ── Sources ────────────────────────────────────────────────────────────

NPI_PRACTICE = {
    "number": "1316521065", "enumeration_type": "NPI-2",
    "basic": {"organization_name": "BRIGHT SMILE DENTAL INC", "status": "A",
              "authorized_official_first_name": "DANA", "authorized_official_last_name": "LEE",
              "authorized_official_title_or_position": "OWNER"},
    "addresses": [
        {"address_purpose": "MAILING", "address_1": "PO BOX 1", "city": "GLENDORA", "state": "CA",
         "postal_code": "917410000"},
        {"address_purpose": "LOCATION", "address_1": "138 N LAKE AVE", "city": "PASADENA", "state": "CA",
         "postal_code": "911011836", "telephone_number": "626-555-0100"}],
    "taxonomies": [{"code": "1223G0001X", "desc": "Dentist, General Practice", "primary": True}],
}


def test_npi_practice_becomes_a_prospect_with_its_owner_as_contact():
    p = to_prospect(NPI_PRACTICE, {"dentist"}, "npi_registry", ("city", "Pasadena"))
    assert (p.name, p.city, p.zip, p.focus_area) == ("Bright Smile Dental Inc", "Pasadena", "91101",
                                                     "dentist_general_practice")
    assert p.metadata["external_ref"] == "1316521065"
    assert p.metadata["contact"] == {"name": "Dana Lee", "title": "Owner", "phone": "(626) 555-0100"}
    assert to_prospect(NPI_PRACTICE, {"chiropractor"}, "npi_registry") is None
    assert to_prospect(NPI_PRACTICE, {"dentist"}, "npi_registry", ("city", "Glendora")) is None  # mailing only
    inactive = {**NPI_PRACTICE, "basic": {**NPI_PRACTICE["basic"], "status": "D"}}
    assert to_prospect(inactive, {"dentist"}, "npi_registry") is None


def test_npi_source_pages_and_dedupes_across_taxonomies():
    seen = []

    def handler(request):
        seen.append(dict(request.url.params))
        return httpx.Response(200, json={"result_count": 1, "results": [NPI_PRACTICE]})

    source = NpiRegistrySource(transport=httpx.MockTransport(handler), pause=0)
    found = list(source.discover({"state": "CA", "npi_taxonomies": ["Dentist", "Dentist, General Practice"],
                                  "cities": ["Pasadena"]}))
    assert len(found) == 1 and len(seen) == 2
    assert seen[0]["enumeration_type"] == "NPI-2" and seen[0]["city"] == "Pasadena"


def test_courtlistener_lists_in_state_civil_litigators_by_case_count(monkeypatch):
    monkeypatch.setenv("COURTLISTENER_API_TOKEN", "tok")
    dockets = [{"docket_id": 11, "attorney_id": [1, 2], "suitNature": "Contract: Other", "dateFiled": "2026-08-01"},
               {"docket_id": 12, "attorney_id": [1], "suitNature": "Civil Rights: Other", "dateFiled": "2026-09-01"},
               {"docket_id": 13, "attorney_id": [3], "suitNature": "Contract: Other", "dateFiled": "2026-09-02"}]
    attorneys = {
        1: {"name": "Avery Attorney", "email": "avery@firm.example", "phone": "415-555-0100",
            "contact_raw": "Avery Attorney\nRoe & Lee LLP\n1 Market St\nSan Francisco, CA 94105\n"},
        2: {"name": "Out Of State", "contact_raw": "Big Firm\n1 Main St\nNew York, NY 10001"},
        3: {"name": "One Case", "contact_raw": "Small Firm\n2 Main St\nOakland, CA 94612"},
    }
    auth = []

    def handler(request):
        auth.append(request.headers.get("authorization"))
        if request.url.path == "/api/rest/v4/search/":
            assert "suitNature:(" in request.url.params["q"]
            return httpx.Response(200, json={"results": dockets, "next": "https://evil.example/next"})
        return httpx.Response(200, json=attorneys[int(request.url.path.rstrip("/").rsplit("/", 1)[1])])

    source = CourtListenerAttorneysSource(transport=httpx.MockTransport(handler), pause=0)
    found = list(source.discover({"state": "CA", "min_cases": 1}))
    assert [p.name for p in found] == ["Avery Attorney", "One Case"]
    avery = found[0]
    assert (avery.city, avery.zip, avery.metadata["firm"], avery.metadata["civil_cases"]) == (
        "San Francisco", "94105", "Roe & Lee LLP", 2)
    assert avery.metadata["contact"]["email"] == "avery@firm.example"
    assert set(auth) == {"Token tok"}
    assert [p.name for p in source.discover({"state": "CA", "min_cases": 2})] == ["Avery Attorney"]


def test_courtlistener_needs_a_token(monkeypatch):
    monkeypatch.delenv("COURTLISTENER_API_TOKEN", raising=False)
    assert CourtListenerAttorneysSource().is_configured() is False


# ── Platform cut ───────────────────────────────────────────────────────


def test_platform_fee_caps_the_contributor_pool(monkeypatch):
    monkeypatch.delenv("AGENCY_OS_PLATFORM_FEE_PCT", raising=False)
    assert (selling.platform_fee_pct(), selling.max_pool_pct()) == (30, 70)
    monkeypatch.setenv("AGENCY_OS_PLATFORM_FEE_PCT", "45")
    assert selling.max_pool_pct() == 55
    monkeypatch.setenv("AGENCY_OS_PLATFORM_FEE_PCT", "nonsense")
    assert selling.platform_fee_pct() == 30


# ── Limits and caching for paid / rate-limited sources ─────────────────

from datetime import date  # noqa: E402

from core.source_budget import BudgetExhausted, SourceBudget  # noqa: E402
from plugins.prospect_sources._attorneys import parse_contact  # noqa: E402
from plugins.prospect_sources.pacer_attorneys import (  # noqa: E402
    PacerAttorneysSource, attorney_key, docket_cents, ecf_court)


@pytest.mark.parametrize("use_db", [False, True])
def test_budget_never_goes_over_and_persists(request, use_db):
    db = request.getfixturevalue("db") if use_db else None
    day = date(2026, 10, 6)
    budget = SourceBudget("s", db, max_requests_per_day=2, max_cents_per_month=300, today=day)
    budget.check(); budget.record(cents=100)
    budget.check(); budget.record(cents=100)
    with pytest.raises(BudgetExhausted, match="2 requests today"):
        budget.check()
    tomorrow = SourceBudget("s", db, max_requests_per_day=2, max_cents_per_month=300, today=date(2026, 10, 7))
    if use_db:  # the month's spending carries over to the next day's run
        tomorrow.check(cents=100)
        with pytest.raises(BudgetExhausted, match=r"\$3\.00 this month"):
            tomorrow.check(cents=101)
    next_month = SourceBudget("s", db, max_cents_per_month=300, today=date(2026, 11, 1))
    next_month.check(cents=300)
    budget.store("k", {"a": 1})
    assert budget.cached("k", 30) == {"a": 1} and budget.cached("missing", 30) is None


def test_contact_block_parsing_finds_phone_and_email_not_fax():
    raw = ("Roe & Lee LLP\n1 Market St\nSan Francisco, CA 94105\nFax: 415-555-0199\n"
           "415-555-0100\nEmail: Avery@RoeLee.example")
    found = parse_contact(raw)
    assert (found["firm"], found["phone"], found["email"]) == ("Roe & Lee LLP", "(415) 555-0100",
                                                               "avery@roelee.example")


def test_courtlistener_stops_at_its_daily_cap_and_resumes_from_cache(db, monkeypatch):
    monkeypatch.setenv("COURTLISTENER_API_TOKEN", "tok")
    monkeypatch.setenv("COURTLISTENER_MAX_REQUESTS_PER_DAY", "2")
    calls = []

    def handler(request):
        calls.append(request.url.path)
        if request.url.path == "/api/rest/v4/search/":
            return httpx.Response(200, json={"results": [
                {"attorney_id": [1, 2], "suitNature": "Contract", "dateFiled": "2026-08-01"}], "next": None})
        attorney_id = request.url.path.rstrip("/").rsplit("/", 1)[1]
        return httpx.Response(200, json={"name": f"Attorney {attorney_id}",
                                         "contact_raw": "Firm\n1 Main St\nOakland, CA 94612"})

    source = CourtListenerAttorneysSource(transport=httpx.MockTransport(handler), pause=0)
    source.bind_db(db)
    assert [p.name for p in source.discover({"state": "CA"})] == ["Attorney 1"]  # search + 1 attorney = cap
    assert len(calls) == 2
    monkeypatch.setenv("COURTLISTENER_MAX_REQUESTS_PER_DAY", "4")
    assert [p.name for p in source.discover({"state": "CA"})] == ["Attorney 1", "Attorney 2"]
    assert calls[2:] == ["/api/rest/v4/search/", "/api/rest/v4/attorneys/2/"]  # attorney 1 came from the cache


def pcl_page(cases, last=True):
    return {"receipt": {"searchFee": ".10"}, "pageInfo": {"last": last}, "content": cases}


CASE_A = {"courtId": "cacdc", "caseId": 101, "natureOfSuit": "190", "dateFiled": "2026-09-01"}
CASE_B = {"courtId": "candc", "caseId": 202, "natureOfSuit": "440", "dateFiled": "2026-09-15"}
AVERY = {"name": "Avery Attorney",
         "contact": "Roe & Lee LLP\n1 Market St\nSan Francisco, CA 94105\n415-555-0100\nEmail: avery@roelee.example"}
NY = {"name": "Nia York", "contact": "Big Firm\n1 Main St\nNew York, NY 10001\nEmail: nia@big.example"}


def pacer_source(dockets, pcl_calls, docket_calls):
    def pcl(request):
        pcl_calls.append((dict(request.url.params), json.loads(request.content), request.headers["x-next-gen-cso"]))
        return httpx.Response(200, json=pcl_page([CASE_A, CASE_B]))

    def fetch(session, court, case_id):
        docket_calls.append((court, case_id))
        return dockets[(court, case_id)], 5000  # two billable pages

    return PacerAttorneysSource(transport=httpx.MockTransport(pcl), login=lambda: ("tok", "session"),
                                docket_fetcher=fetch)


def test_pacer_lists_civil_litigators_and_records_what_it_spends(db, monkeypatch):
    monkeypatch.delenv("PACER_MAX_USD_PER_MONTH", raising=False)
    dockets = {("cacd", "101"): [AVERY, NY], ("cand", "202"): [AVERY]}
    pcl_calls, docket_calls = [], []
    source = pacer_source(dockets, pcl_calls, docket_calls)
    source.bind_db(db)
    found = list(source.discover({"state": "CA"}))
    assert [p.name for p in found] == ["Avery Attorney"]  # Nia York is out of state
    avery = found[0]
    assert avery.metadata["civil_cases"] == 2
    assert set(avery.metadata["suit_natures"]) == {"Contract: Other", "Civil Rights: Other"}
    assert avery.metadata["contact"] == {"name": "Avery Attorney", "title": "Attorney",
                                         "email": "avery@roelee.example", "phone": "(415) 555-0100"}
    params, body, token = pcl_calls[0]
    assert params == {"page": "0"} and token == "tok"
    assert body["jurisdictionType"] == "cv" and body["courtId"] == ["cac", "can", "cas", "cae"]
    assert "190" in body["natureOfSuit"] and "550" not in body["natureOfSuit"]  # no prisoner petitions
    assert docket_calls == [("cacd", "101"), ("cand", "202")]
    assert source.budget().cents_this_month() == 10 + 20 + 20  # one search page + two 2-page dockets

    # Re-running pays for nothing: the search page and both dockets are cached.
    assert [p.name for p in source.discover({"state": "CA"})] == ["Avery Attorney"]
    assert len(pcl_calls) == 1 and len(docket_calls) == 2


def test_pacer_spending_cap_leaves_room_for_a_full_docket(db, monkeypatch):
    # $0.10 search + a $0.20 docket leaves $2.85: less than the $3.00 a docket can cost, so it stops.
    monkeypatch.setenv("PACER_MAX_USD_PER_MONTH", "3.15")
    dockets = {("cacd", "101"): [AVERY], ("cand", "202"): [AVERY]}
    pcl_calls, docket_calls = [], []
    source = pacer_source(dockets, pcl_calls, docket_calls)
    source.bind_db(db)
    assert [p.metadata["civil_cases"] for p in source.discover({"state": "CA"})] == [1]
    assert docket_calls == [("cacd", "101")] and source.budget().cents_this_month() == 30


def test_pacer_helpers():
    assert (ecf_court("cacdc"), ecf_court("candc")) == ("cacd", "cand")
    assert (docket_cents(0), docket_cents(4320), docket_cents(4321), docket_cents(10**7)) == (10, 10, 20, 300)
    assert attorney_key("Avery  Attorney", AVERY["contact"]) == attorney_key("avery attorney", "avery@roelee.example")
    assert attorney_key("Avery Attorney", "x 94105") != attorney_key("Avery Attorney", "x 10001")
