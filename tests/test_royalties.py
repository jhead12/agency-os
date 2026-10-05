"""
Rep royalties (core/royalties.py): credit from our records, accruals on
sales and royalties, the guarantee period, clawbacks, and payouts.

Run: TEST_DATABASE_URL=postgresql://localhost/agency_os_test python -m pytest tests/test_royalties.py
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import royalties  # noqa: E402
from tests.test_access import client_for, db, make_user  # noqa: E402,F401
from tests.test_selling import buy, claim, org, publish, setup_list, slug_of, store  # noqa: E402,F401

ADDRESS = "0x" + "a1" * 20


def test_split_follows_task_weights():
    assert royalties.split(1_000_000, {"enriched": [1], "qualified": [2], "sourced": [3]}) == {
        (1, "enriched"): 500_000, (2, "qualified"): 300_000, (3, "sourced"): 200_000}
    assert royalties.split(1_000_000, {"enriched": [1, 2]}) == {(1, "enriched"): 250_000, (2, "enriched"): 250_000}
    assert royalties.split(1_000_000, {}) == {}  # nobody credited: the house keeps it
    # The plan's example: a 20% pool on a $1.00 royalty pays $0.10 / $0.06 / $0.04.
    pool = 1_000_000 * 20 // 100
    assert sorted(royalties.split(pool, {"enriched": [1], "qualified": [2], "sourced": [3]}).values()) == [
        40_000, 60_000, 100_000]


def credited_lead(db):
    """A sold-list lead that rep A enriched (contact edit), rep B qualified (a call), owner credited C as sourcing."""
    owner, campaign_id, list_id, good, _ = setup_list(db)
    a = db.load_current_user(make_user(db, "a@x.com", "Sales Rep"))
    b = db.load_current_user(make_user(db, "b@x.com", "Caller"))
    c = db.load_current_user(make_user(db, "c@x.com", "Sales Rep"))
    pid = good[0]
    oid = db.conn.execute("SELECT id FROM outreach WHERE prospect_id = ?", (pid,)).fetchone()["id"]
    client_for("a@x.com").post(f"/prospects/{pid}/contact",
                               data={"outreach_id": oid, "contact_email": "dana.new@org0.example"})
    client_for("b@x.com").post("/call-log/record", data={
        "prospect_id": pid, "outreach_id": oid, "campaign_id": campaign_id, "outcome": "completed",
        "decision_maker_name": "Dana Director"})
    royalties.set_credit(db, owner, pid, c.id, "sourced", True)
    return owner, list_id, pid, (a, b, c)


def test_credit_comes_from_our_records_and_owners(db):
    owner, _list_id, pid, (a, b, c) = credited_lead(db)
    credit = {(r["user_id"], r["task"], r["active"]) for r in royalties.credits(db, pid)}
    assert credit == {(a.id, "enriched", 1), (b.id, "qualified", 1), (c.id, "sourced", 1)}
    royalties.set_credit(db, owner, pid, b.id, "qualified", False)
    assert (b.id, "qualified", 0) in {(r["user_id"], r["task"], r["active"]) for r in royalties.credits(db, pid)}
    with pytest.raises(ValueError):
        royalties.set_credit(db, owner, pid, b.id, "cheerleading", True)


def accruals(db):
    return {(r["user_id"], r["income"], r["status"]): r["amount_atomic"] for r in db.conn.execute(
        "SELECT * FROM rep_accruals ORDER BY id").fetchall()}


def test_sales_accrue_mature_and_claw_back(db, store):
    owner, list_id, pid, (a, b, c) = credited_lead(db)
    publish(db, owner, list_id)  # $5 unlock over 5 leads; royalties $0.25 connected / $1 pitched; 20% pool
    client, slug = client_for(), slug_of(db)
    sale = buy(client, slug)
    lead = next(l for l in sale["leads"] if l["contact_email"] == "dana.new@org0.example")
    client.post(f"/x402/packages/{slug}/contacts", json={"lead_id": lead["lead_id"]},
                headers={"PAYMENT-SIGNATURE": "fake-signature"})
    # $1.00 of the unlock per lead -> $0.20 pool. Rep B reached the decision-maker, so the
    # lead is "pitched" and its royalty is $1.00 -> another $0.20 pool.
    assert lead["tier"] == "pitched"
    assert accruals(db) == {
        (a.id, "unlock", "pending"): 100_000, (b.id, "unlock", "pending"): 60_000, (c.id, "unlock", "pending"): 40_000,
        (a.id, "royalty", "pending"): 100_000, (b.id, "royalty", "pending"): 60_000, (c.id, "royalty", "pending"): 40_000}
    mine = royalties.balances(db, a.id)[0]
    assert (mine["pending"], mine["payable"]) == (200_000, 0)

    # After the buyer's window and claim period: payable.
    db.conn.execute("UPDATE rep_accruals SET payable_at = CURRENT_TIMESTAMP - INTERVAL '1 day'")
    assert royalties.balances(db, a.id)[0]["payable"] == 200_000

    # A claim on that lead holds: the share is taken back with a negative line.
    org(db, db.conn.execute("SELECT id FROM campaigns WHERE name = 'sell-test'").fetchone()["id"], 30)
    assert claim(client, slug, sale["claim_token"], [lead["lead_id"]])["remedy"] == "replacement"
    assert royalties.balances(db, a.id)[0]["payable"] == 0


def test_claims_reverse_pending_shares(db, store):
    owner, list_id, pid, (a, _b, _c) = credited_lead(db)
    publish(db, owner, list_id)
    client, slug = client_for(), slug_of(db)
    sale = buy(client, slug)
    lead = next(l for l in sale["leads"] if l["contact_email"] == "dana.new@org0.example")
    org(db, db.conn.execute("SELECT id FROM campaigns WHERE name = 'sell-test'").fetchone()["id"], 31)
    claim(client, slug, sale["claim_token"], [lead["lead_id"]])
    assert accruals(db)[(a.id, "unlock", "reversed")] == 100_000
    assert royalties.balances(db, a.id)[0]["pending"] == 0


class FakeSender:
    def __init__(self, fail=False):
        self.fail, self.sent = fail, []

    def is_configured(self):
        return True

    def send(self, to, amount, network):
        if self.fail:
            raise RuntimeError("insufficient funds")
        self.sent.append((to, amount, network))
        return "0x" + f"{len(self.sent):064x}"


def payable_rep(db, store, monkeypatch):
    monkeypatch.setenv("AGENCY_OS_PAYOUT_MIN_USD", "0.05")
    owner, list_id, _pid, (a, b, c) = credited_lead(db)
    publish(db, owner, list_id)
    buy(client_for(), slug_of(db))
    db.conn.execute("UPDATE rep_accruals SET payable_at = CURRENT_TIMESTAMP - INTERVAL '1 day'")
    return owner, a


def test_payouts_send_once_and_record(db, store, monkeypatch):
    owner, a = payable_rep(db, store, monkeypatch)
    sender = FakeSender()
    assert "payout address" in royalties.pay(db, owner, a.id, sender=sender)
    assert royalties.set_payout_address(db, a, "not-an-address").startswith("Enter a 0x")
    assert royalties.set_payout_address(db, a, ADDRESS) == ""
    assert royalties.pay(db, owner, a.id, sender=sender) == ""
    assert sender.sent == [(ADDRESS, 100_000, "base-sepolia")]
    assert royalties.balances(db, a.id)[0]["paid"] == 100_000
    assert "minimum" in royalties.pay(db, owner, a.id, sender=sender)  # nothing left: can't pay twice
    assert db.conn.execute("SELECT 1 FROM spend WHERE kind = 'contributor_payout' AND user_id = ?", (a.id,)).fetchone()


def test_failed_transfer_releases_the_balance_and_hand_payments(db, store, monkeypatch):
    owner, a = payable_rep(db, store, monkeypatch)
    royalties.set_payout_address(db, a, ADDRESS)
    assert "insufficient funds" in royalties.pay(db, owner, a.id, sender=FakeSender(fail=True))
    assert royalties.balances(db, a.id)[0]["payable"] == 100_000  # still owed
    assert "Automatic payouts are off" in royalties.pay(db, owner, a.id, sender=None)
    assert royalties.pay(db, owner, a.id, tx_hash="0x" + "cd" * 32) == ""
    assert royalties.balances(db, a.id)[0]["paid"] == 100_000
    monkeypatch.setenv("AGENCY_OS_PAYOUT_MAX_USD", "0.01")
    db.conn.execute("UPDATE rep_accruals SET status = 'payable', payout_id = NULL")  # pretend it's owed again
    assert "cap" in royalties.pay(db, owner, a.id, sender=FakeSender())


def test_pages(db, store, monkeypatch):
    owner, a = payable_rep(db, store, monkeypatch)
    rep = client_for("a@x.com")
    account = rep.get("/account").text
    assert "My data royalties" in account and "$0.10" in account
    bad = rep.post("/account/payout-address", data={"address": ADDRESS, "current_password": "wrong"})
    assert "incorrect" in bad.headers["location"]
    rep.post("/account/payout-address", data={"address": ADDRESS, "current_password": "correct-horse-battery"})
    assert royalties.balances(db, a.id)[0]["payout_address"] == ADDRESS

    owner_client = client_for("owner@x.com")
    page = owner_client.get("/admin/payouts").text
    assert "Balances" in page and "Record payout" in page
    assert rep.get("/admin/payouts").status_code == 403
    owner_client.post(f"/admin/payouts/{a.id}", data={"tx_hash": "0x" + "ef" * 32})
    assert royalties.balances(db, a.id)[0]["paid"] == 100_000
    pid = db.conn.execute("SELECT prospect_id FROM lead_contributions LIMIT 1").fetchone()["prospect_id"]
    assert "Who built this lead" in owner_client.get(f"/prospects/{pid}").text
    assert rep.post(f"/prospects/{pid}/credit", data={"user_id": a.id, "task": "sourced"}).status_code == 403
