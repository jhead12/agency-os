"""
Paid searches (core/searches.py): creating them idempotently, reading and
cancelling them, max_results, billing only new prospects, and ?search=.

Run: TEST_DATABASE_URL=postgresql://localhost/agency_os_test python -m pytest tests/test_searches.py
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import accounts, searches  # noqa: E402
from core.models import Prospect  # noqa: E402
from tests.test_access import client_for, db  # noqa: E402,F401

CITY = {"type": "city", "params": {"city": "Austin", "state": "TX", "query": "dentist"},
        "max_results": 3, "idempotency_key": "u9-search-1"}


@pytest.fixture
def account(db):
    found, key = accounts.create(db, "org_a", "A")
    return found, {"Authorization": f"Bearer {key}"}


def shop(n):
    return Prospect(name=f"Shop {n}", city="Austin", state="TX", source="city_search")


def test_create_is_idempotent_and_conflicts_on_different_settings(db, account):
    _, auth = account
    c = client_for()
    r = c.post("/api/v1/searches", json=CITY, headers=auth)
    assert r.status_code == 202
    search = r.json()["search"]
    assert (search["status"], search["delivered"], search["max_results"]) == ("queued", 0, 3)
    assert search["params"] == CITY["params"]
    again = c.post("/api/v1/searches", json=CITY, headers=auth)
    assert again.status_code == 200 and again.json()["search"]["id"] == search["id"]
    # Same params in another key order is the same search.
    reordered = {**CITY, "params": {"query": "dentist", "state": "TX", "city": "Austin"}}
    assert c.post("/api/v1/searches", json=reordered, headers=auth).status_code == 200
    r = c.post("/api/v1/searches", json={**CITY, "max_results": 50}, headers=auth)
    assert r.status_code == 409 and r.json()["error"] == "conflict"
    assert db.conn.execute("SELECT COUNT(*) FROM searches").fetchone()[0] == 1


@pytest.mark.parametrize("change", [
    {"type": "phonebook"},
    {"params": "Austin"},
    {"params": {"city": "Austin", "state": "TX"}},
    {"type": "rss", "params": {}},
    {"type": "scrape", "params": {"url": "https://example.com", "item_selector": "li"}},
    {"params": {"city": "Austin", "state": "TX", "query": "x" * 5000}},
    {"max_results": 0}, {"max_results": 501}, {"max_results": "10"}, {"max_results": True},
    {"idempotency_key": ""}, {"idempotency_key": "has spaces"},
])
def test_bad_searches_are_rejected(db, account, change):
    _, auth = account
    r = client_for().post("/api/v1/searches", json={**CITY, **change}, headers=auth)
    assert r.status_code == 422 and r.json()["error"] == "invalid"


def test_searches_need_an_active_account_key(db, account):
    c = client_for()
    assert c.post("/api/v1/searches", json=CITY).status_code == 401
    accounts.set_status(db, "org_a", "suspended")
    assert c.post("/api/v1/searches", json=CITY, headers=account[1]).status_code == 403


def test_an_account_cannot_see_or_cancel_another_accounts_search(db, account):
    _, auth = account
    _, other_key = accounts.create(db, "org_b", "B")
    other = {"Authorization": f"Bearer {other_key}"}
    c = client_for()
    search_id = c.post("/api/v1/searches", json=CITY, headers=auth).json()["search"]["id"]
    assert c.get(f"/api/v1/searches/{search_id}", headers=other).status_code == 404
    assert c.post(f"/api/v1/searches/{search_id}/cancel", headers=other).status_code == 404
    assert c.get(f"/api/v1/prospects?search={search_id}", headers=other).status_code == 404
    assert c.get(f"/api/v1/searches/{search_id}", headers=auth).json()["search"]["status"] == "queued"
    # The same idempotency_key in another account is a separate search.
    assert c.post("/api/v1/searches", json=CITY, headers=other).status_code == 202


def test_cancelling_queued_ends_it_and_running_asks_it_to_stop(db, account):
    _, auth = account
    c = client_for()
    queued = c.post("/api/v1/searches", json=CITY, headers=auth).json()["search"]["id"]
    r = c.post(f"/api/v1/searches/{queued}/cancel", headers=auth).json()["search"]
    assert (r["status"], r["cancel_requested"]) == ("canceled", True) and r["finished_at"]

    running = c.post("/api/v1/searches", json={**CITY, "idempotency_key": "u9-search-2"},
                     headers=auth).json()["search"]["id"]
    db.conn.execute("UPDATE searches SET status = 'running' WHERE id = ?", (running,))
    r = c.post(f"/api/v1/searches/{running}/cancel", headers=auth).json()["search"]
    assert (r["status"], r["cancel_requested"], r["finished_at"]) == ("running", True, None)

    db.conn.execute("UPDATE searches SET status = 'done' WHERE id = ?", (running,))
    assert c.post(f"/api/v1/searches/{running}/cancel", headers=auth).json()["search"]["status"] == "done"


def test_delivered_counts_only_new_prospects_and_stops_at_max_results(db, account):
    found, auth = account
    earlier = accounts.add_prospect(db, found["id"], shop(0))
    search, _ = searches.create(db, found["id"], CITY)
    assert searches.save_result(db, search, shop(0)) is False      # the account already had it
    assert searches.save_result(db, search, shop(1)) is True
    assert searches.save_result(db, search, shop(1)) is False      # found twice in one search
    assert searches.save_result(db, search, shop(2)) is True
    assert searches.save_result(db, search, shop(3)) is True
    assert searches.save_result(db, search, shop(4)) is None       # max_results reached
    assert searches.get(db, found["id"], search["id"])["delivered"] == 3

    c = client_for()
    listed = c.get(f"/api/v1/prospects?search={search['id']}", headers=auth).json()["prospects"]
    assert [p["name"] for p in listed] == ["Shop 0", "Shop 1", "Shop 2", "Shop 3"]
    assert listed[0]["id"] == earlier
    everything = c.get("/api/v1/prospects", headers=auth).json()["prospects"]
    assert len(everything) == 4
    assert db.prospect_hidden(listed[1]["id"], ())


def test_prospects_found_by_another_accounts_search_are_billed_to_each(db, account):
    a, _ = account
    b, _ = accounts.create(db, "org_b", "B")
    search_a, _ = searches.create(db, a["id"], CITY)
    search_b, _ = searches.create(db, b["id"], CITY)
    assert searches.save_result(db, search_a, shop(1)) is True
    assert searches.save_result(db, search_b, shop(1)) is True
    assert [p["name"] for p in accounts.prospects(db, b["id"], search_id=search_b["id"])] == ["Shop 1"]
