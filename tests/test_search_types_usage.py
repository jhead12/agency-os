"""
Search types listed from the plugins (GET /api/v1/search-types), usage per
account per day (GET /api/v1/usage) and the daily cap (task B4).

Run: TEST_DATABASE_URL=postgresql://localhost/agency_os_test python -m pytest tests/test_search_types_usage.py
"""

import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import accounts, searches  # noqa: E402
from tests.test_access import client_for, db  # noqa: E402,F401

CITY = {"type": "city", "params": {"city": "Austin", "state": "TX", "query": "dentist"},
        "max_results": 3, "idempotency_key": "u9-search-1"}


@pytest.fixture
def platform(monkeypatch):
    key, digest = accounts.new_platform_key()
    monkeypatch.setenv("AGENCY_OS_PLATFORM_KEY_HASH", digest)
    return {"Authorization": f"Bearer {key}"}


@pytest.fixture
def account(db):
    found, key = accounts.create(db, "org_a", "A")
    return found, {"Authorization": f"Bearer {key}"}


def finish(db, search_id, delivered, pages=1, cost=0, days_ago=0):
    db.conn.execute(
        """UPDATE searches SET status = 'done', delivered = ?, pages_fetched = ?, cost_cents = ?,
                  created_at = CURRENT_TIMESTAMP - make_interval(days => ?) WHERE id = ?""",
        (delivered, pages, cost, days_ago, search_id))


# ── Search types ───────────────────────────────────────────────────────


def test_search_types_come_from_the_plugins(db, account, platform):
    c = client_for()
    assert c.get("/api/v1/search-types").status_code == 401
    listed = c.get("/api/v1/search-types", headers=platform).json()["search_types"]
    assert [t["type"] for t in listed] == ["city", "rss", "scrape"]
    city = listed[0]
    assert city["label"] == "Businesses in a city" and city["attribution"].startswith("© OpenStreetMap")
    assert [(f["name"], f["required"]) for f in city["fields"]] == [("city", True), ("state", True), ("query", True)]
    assert city["max_results"] == searches.MAX_RESULTS
    # An account key works too, also a suspended one (it may still look at its results).
    accounts.set_status(db, "org_a", "suspended")
    assert c.get("/api/v1/search-types", headers=account[1]).json()["search_types"] == listed


def test_each_plugins_required_fields_are_what_create_requires():
    for kind, plugin in searches.plugins().items():
        assert set(searches.required_params(kind, searches.plugins())) == set(searches.REQUIRED_PARAMS[kind])


class NewsletterSearch:
    key = "newsletter"
    label = "Newsletter signups"
    description = "test"
    fields = [{"name": "list_id", "label": "List", "type": "text", "required": True}]
    attribution = None

    def validate(self, params):
        return {"list_id": str(params["list_id"])}

    def run(self, params, ctx):
        return iter(())


def test_a_new_search_plugin_needs_no_core_change(db, account):
    available = {**searches.plugins(), "newsletter": NewsletterSearch()}
    assert "newsletter" in [t["type"] for t in searches.types(available)]
    body = {"type": "newsletter", "params": {"list_id": 7}, "max_results": 5, "idempotency_key": "n-1"}
    search, created = searches.create(db, account[0]["id"], body, available)
    assert created and search["type"] == "newsletter"
    with pytest.raises(searches.SearchError, match="needs params: list_id"):
        searches.create(db, account[0]["id"], {**body, "params": {}, "idempotency_key": "n-2"}, available)


# ── Usage ──────────────────────────────────────────────────────────────


def test_usage_totals_equal_the_sum_of_searches(db, account, platform):
    _, auth = account
    other, other_key = accounts.create(db, "org_b", "B")
    c = client_for()
    ids = [c.post("/api/v1/searches", json={**CITY, "idempotency_key": f"k{i}"}, headers=auth).json()["search"]["id"]
           for i in range(3)]
    finish(db, ids[0], delivered=3, pages=2, cost=5)
    finish(db, ids[1], delivered=1, pages=1, cost=1)
    finish(db, ids[2], delivered=2, pages=4, cost=0, days_ago=1)
    b_id = c.post("/api/v1/searches", json=CITY, headers={"Authorization": f"Bearer {other_key}"}).json()["search"]["id"]
    finish(db, b_id, delivered=1)

    today = date.today()
    everyone = c.get("/api/v1/usage", headers=platform).json()
    rows = {(r["external_ref"], r["day"]): r for r in everyone["usage"]}
    assert rows[("org_a", today.isoformat())] == {
        "external_ref": "org_a", "day": today.isoformat(), "searches": 2, "delivered": 4,
        "pages_fetched": 3, "api_requests": 0, "cost_cents": 6}
    assert rows[("org_a", (today - timedelta(days=1)).isoformat())]["delivered"] == 2
    assert rows[("org_b", today.isoformat())]["delivered"] == 1
    total = db.conn.execute("SELECT SUM(delivered) FROM searches").fetchone()[0]
    assert sum(r["delivered"] for r in everyone["usage"]) == total == 7

    # An account key sees only its own; a date range narrows it.
    own = c.get(f"/api/v1/usage?from={today}&to={today}", headers=auth).json()["usage"]
    assert [(r["external_ref"], r["delivered"]) for r in own] == [("org_a", 4)]


def test_usage_refuses_bad_keys_and_ranges(db, account, platform):
    c = client_for()
    assert c.get("/api/v1/usage").status_code == 401
    assert c.get("/api/v1/usage?from=yesterday", headers=platform).status_code == 422
    assert c.get("/api/v1/usage?from=2026-10-02&to=2026-10-01", headers=platform).status_code == 422
    assert c.get("/api/v1/usage?from=2024-01-01&to=2026-10-01", headers=platform).status_code == 422


# ── Daily cap ──────────────────────────────────────────────────────────


def test_the_daily_cap_refuses_a_search_and_does_not_create_it(db, account, monkeypatch):
    monkeypatch.setenv("AGENCY_OS_ACCOUNT_DAILY_PROSPECTS", "10")
    _, auth = account
    c = client_for()
    first = c.post("/api/v1/searches", json={**CITY, "max_results": 6}, headers=auth)
    assert first.status_code == 202  # 6 reserved while it's queued
    over = c.post("/api/v1/searches", json={**CITY, "max_results": 5, "idempotency_key": "k2"}, headers=auth)
    assert over.status_code == 429 and over.json()["error"] == "limit_reached"
    assert "4 left today" in over.json()["message"]
    assert db.conn.execute("SELECT COUNT(*) FROM searches").fetchone()[0] == 1

    # A retry of an accepted search is never refused by the cap.
    assert c.post("/api/v1/searches", json={**CITY, "max_results": 6}, headers=auth).status_code == 200

    # Once it finishes, only what it delivered counts.
    finish(db, first.json()["search"]["id"], delivered=2)
    assert c.post("/api/v1/searches", json={**CITY, "max_results": 8, "idempotency_key": "k3"},
                  headers=auth).status_code == 202


def test_yesterdays_searches_dont_count_toward_today(db, account, monkeypatch):
    monkeypatch.setenv("AGENCY_OS_ACCOUNT_DAILY_PROSPECTS", "5")
    _, auth = account
    c = client_for()
    old = c.post("/api/v1/searches", json={**CITY, "max_results": 5}, headers=auth).json()["search"]["id"]
    finish(db, old, delivered=5, days_ago=1)
    assert c.post("/api/v1/searches", json={**CITY, "max_results": 5, "idempotency_key": "k2"},
                  headers=auth).status_code == 202
