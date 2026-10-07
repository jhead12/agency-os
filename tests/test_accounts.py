"""
Customer accounts (core/accounts.py): the platform and account keys, the
/api/v1 account endpoints, and keeping account prospects out of house views.

Run: TEST_DATABASE_URL=postgresql://localhost/agency_os_test python -m pytest tests/test_accounts.py
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import accounts, selling, tools  # noqa: E402
from core.models import Prospect  # noqa: E402
from tests.test_access import client_for, db, make_user  # noqa: E402,F401


@pytest.fixture
def platform(monkeypatch):
    key, digest = accounts.new_platform_key()
    monkeypatch.setenv("AGENCY_OS_PLATFORM_KEY_HASH", digest)
    return {"Authorization": f"Bearer {key}"}


def bearer(key):
    return {"Authorization": f"Bearer {key}"}


def found(n, **kw):
    return Prospect(name=f"Shop {n}", city="Austin", state="TX", source="city_search", **kw)


# ── Platform key ───────────────────────────────────────────────────────


def test_account_api_is_off_without_a_platform_key_hash(db, monkeypatch):
    monkeypatch.delenv("AGENCY_OS_PLATFORM_KEY_HASH", raising=False)
    r = client_for().post("/api/v1/accounts", json={"external_ref": "org_1", "name": "Acme"})
    assert r.status_code == 503


def test_wrong_platform_key_is_refused(db, platform):
    c = client_for()
    assert c.post("/api/v1/accounts", json={"external_ref": "org_1", "name": "Acme"}).status_code == 401
    r = c.post("/api/v1/accounts", json={"external_ref": "org_1", "name": "Acme"},
               headers=bearer("aos_plat_wrong"))
    assert r.status_code == 401


def test_create_is_idempotent_and_only_returns_the_key_once(db, platform):
    c = client_for()
    r = c.post("/api/v1/accounts", json={"external_ref": "org_1", "name": "Acme"}, headers=platform)
    assert r.status_code == 201
    key = r.json()["key"]
    assert key.startswith("aos_acct_") and r.json()["account"]["key_hint"] == key[-4:]
    again = c.post("/api/v1/accounts", json={"external_ref": "org_1", "name": "Acme"}, headers=platform)
    assert again.status_code == 200 and "key" not in again.json()
    assert c.get("/api/v1/account", headers=bearer(key)).json()["account"]["external_ref"] == "org_1"
    assert db.conn.execute("SELECT key_hash FROM accounts").fetchone()["key_hash"] != key


def test_bad_account_input_is_rejected(db, platform):
    c = client_for()
    assert c.post("/api/v1/accounts", json={"external_ref": "bad ref!", "name": "A"},
                  headers=platform).status_code == 422
    assert c.post("/api/v1/accounts", json={"external_ref": "org_1"}, headers=platform).status_code == 422
    assert c.post("/api/v1/accounts", content=b"[1]", headers=platform).status_code == 422


def test_rotating_a_key_retires_the_old_one(db, platform):
    c = client_for()
    old = c.post("/api/v1/accounts", json={"external_ref": "org_1", "name": "Acme"}, headers=platform).json()["key"]
    new = c.post("/api/v1/accounts/org_1/key", headers=platform).json()["key"]
    assert c.get("/api/v1/account", headers=bearer(old)).status_code == 401
    assert c.get("/api/v1/account", headers=bearer(new)).status_code == 200
    assert c.post("/api/v1/accounts/nobody/key", headers=platform).status_code == 404


def test_a_suspended_account_is_refused_until_reactivated(db, platform):
    c = client_for()
    key = c.post("/api/v1/accounts", json={"external_ref": "org_1", "name": "Acme"}, headers=platform).json()["key"]
    r = c.post("/api/v1/accounts/org_1/status", json={"status": "suspended"}, headers=platform)
    assert r.status_code == 200 and r.json()["account"]["status"] == "suspended"
    assert c.get("/api/v1/account", headers=bearer(key)).status_code == 403
    assert c.get("/api/v1/prospects", headers=bearer(key)).status_code == 403
    c.post("/api/v1/accounts/org_1/status", json={"status": "active"}, headers=platform)
    assert c.get("/api/v1/account", headers=bearer(key)).status_code == 200
    assert c.post("/api/v1/accounts/org_1/status", json={"status": "gone"}, headers=platform).status_code == 422
    assert c.post("/api/v1/accounts/nobody/status", json={"status": "active"},
                  headers=platform).status_code == 404


def test_an_account_key_is_not_a_platform_key(db, platform):
    c = client_for()
    key = c.post("/api/v1/accounts", json={"external_ref": "org_1", "name": "Acme"}, headers=platform).json()["key"]
    assert c.post("/api/v1/accounts", json={"external_ref": "org_2", "name": "B"},
                  headers=bearer(key)).status_code == 401


# ── An account's prospects ─────────────────────────────────────────────


def test_each_account_sees_only_its_own_prospects_paged(db):
    a, key_a = accounts.create(db, "org_a", "A")
    b, key_b = accounts.create(db, "org_b", "B")
    ids = [accounts.add_prospect(db, a["id"], found(n)) for n in range(3)]
    accounts.add_prospect(db, b["id"], found(9))
    c = client_for()
    page = c.get("/api/v1/prospects?limit=2", headers=bearer(key_a)).json()
    assert [p["id"] for p in page["prospects"]] == ids[:2] and page["next_after"] == ids[1]
    rest = c.get(f"/api/v1/prospects?limit=2&after={ids[1]}", headers=bearer(key_a)).json()
    assert [p["id"] for p in rest["prospects"]] == ids[2:] and rest["next_after"] is None
    assert [p["name"] for p in c.get("/api/v1/prospects", headers=bearer(key_b)).json()["prospects"]] == ["Shop 9"]


def test_account_prospects_stay_out_of_house_views(db):
    owner = make_user(db, "owner@example.com", "Owner")
    account, _ = accounts.create(db, "org_a", "A")
    private = accounts.add_prospect(db, account["id"], found(1))
    house = db.upsert_prospect(Prospect(name="House Org", city="Austin", state="TX", source="irs_bmf"))

    page = client_for("owner@example.com").get("/prospects")
    assert "House Org" in page.text and "Shop 1" not in page.text
    assert client_for("owner@example.com").get(f"/prospects/{private}").status_code == 404
    assert db.prospect_hidden(private, ()) and not db.prospect_hidden(house, ())
    assert selling.candidates(db, {}) == [house]
    user = db.load_current_user(owner)
    assert [p["id"] for p in tools._search_prospects(db, user, {})["prospects"]] == [house]


def test_an_account_never_overwrites_a_house_or_other_account_prospect(db):
    house = db.upsert_prospect(Prospect(name="Shop 1", city="Austin", state="TX", source="irs_bmf",
                                        website_url="https://house.example"))
    a, _ = accounts.create(db, "org_a", "A")
    b, _ = accounts.create(db, "org_b", "B")
    assert accounts.add_prospect(db, a["id"], found(1, website_url="https://scraped.example")) == house
    row = db.conn.execute("SELECT account_id, website_url, source FROM prospects WHERE id = ?", (house,)).fetchone()
    assert (row["account_id"], row["website_url"], row["source"]) == (None, "https://house.example", "irs_bmf")
    assert [p["id"] for p in accounts.prospects(db, a["id"])] == [house]

    mine = accounts.add_prospect(db, a["id"], found(2))
    accounts.add_prospect(db, b["id"], found(2, website_url="https://b.example"))
    row = db.conn.execute("SELECT account_id, website_url FROM prospects WHERE id = ?", (mine,)).fetchone()
    assert (row["account_id"], row["website_url"]) == (a["id"], None)
    assert [p["id"] for p in accounts.prospects(db, b["id"])] == [mine]


def test_the_house_finding_an_account_prospect_makes_it_a_house_prospect(db):
    a, _ = accounts.create(db, "org_a", "A")
    pid = accounts.add_prospect(db, a["id"], found(1))
    assert db.prospect_hidden(pid, ())
    assert db.upsert_prospect(found(1)) == pid
    assert not db.prospect_hidden(pid, ())
    assert [p["id"] for p in accounts.prospects(db, a["id"])] == [pid]
