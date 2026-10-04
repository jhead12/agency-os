"""
Selling our own lists (core/selling.py): publishing, the x402 endpoints,
royalties, incoming claims, do-not-sell, and the real x402 SDK payment path.

The facilitator and wallets are fakes; nothing touches the network.

Run: TEST_DATABASE_URL=postgresql://localhost/agency_os_test python -m pytest tests/test_selling.py
"""

import base64
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import access, lead_packages, selling  # noqa: E402
from core.models import CallLog, Prospect  # noqa: E402
from core.payments import BASE_SEPOLIA, USDC  # noqa: E402
from tests.test_access import client_for, db, make_user  # noqa: E402,F401

PAY_TO = "0x" + "5e" * 20
BUYER = "0x" + "cd" * 20


def b64(data) -> str:
    return base64.b64encode(json.dumps(data).encode()).decode()


class FakeGate:
    """Accepts PAYMENT-SIGNATURE "fake-signature" as a payment from `payer`."""

    def __init__(self, payer=BUYER):
        self.payer, self.settled = payer, []

    def payment_required(self, amount, url, description):
        return b64({"x402Version": 2, "resource": {"url": url}, "accepts": [{
            "scheme": "exact", "network": BASE_SEPOLIA, "asset": USDC[BASE_SEPOLIA], "amount": str(amount),
            "payTo": PAY_TO, "maxTimeoutSeconds": 300, "extra": {"name": "USDC", "version": "2"}}]})

    def verify(self, header, amount, url):
        return selling.Verified(header == "fake-signature", payer=self.payer, error="bad payment", context=amount)

    def settle(self, verified):
        tx = "0x" + f"{len(self.settled) + 1:064x}"
        self.settled.append(verified.context)
        return selling.Settled(True, tx, b64({"success": True, "transaction": tx, "network": BASE_SEPOLIA}))


@pytest.fixture
def store(monkeypatch):
    monkeypatch.setenv("AGENCY_OS_SELL", "on")
    monkeypatch.setenv("AGENCY_OS_SELL_PAY_TO", PAY_TO)
    monkeypatch.delenv("AGENCY_OS_SELL_NETWORK", raising=False)
    gate = FakeGate()
    monkeypatch.setattr(selling, "_gate", gate)
    return gate


def org(db, campaign_id, n, *, called=True, email=True, do_not_sell=False, source="irs_bmf"):
    pid = db.upsert_prospect(Prospect(name=f"Org {n}", state="CA", city="Los Angeles", source=source))
    oid = db.upsert_outreach(pid, campaign_id)
    db.update_outreach(oid, {"contact_name": f"Dana {n}", "contact_phone": "213-555-0100",
                             **({"contact_email": f"dana{n}@org{n}.example"} if email else {})})
    if called:
        db.log_call(CallLog(outreach_id=oid, campaign_id=campaign_id, prospect_id=pid, outcome="completed",
                            called_by="Ana"))
    if do_not_sell:
        db.conn.execute("UPDATE prospects SET do_not_sell = 1 WHERE id = ?", (pid,))
    return pid


def setup_list(db):
    owner_id = make_user(db, "owner@x.com", access.OWNER_ROLE)
    owner = db.load_current_user(owner_id)
    campaign_id = db.upsert_campaign("sell-test", "x")
    good = [org(db, campaign_id, n) for n in range(5)]
    unworked = org(db, campaign_id, 10, called=False)
    blocked = org(db, campaign_id, 11, do_not_sell=True)
    bought = org(db, campaign_id, 12, source="x402:leads.test")
    db.save_prospect_list(owner_id, "LA orgs", {"campaign": "sell-test"})
    list_id = db.list_prospect_saved_lists(owner_id)[0]["id"]
    return owner, campaign_id, list_id, good, (unworked, blocked, bought)


def publish(db, owner, list_id, **kw):
    args = dict(saved_list_id=list_id, title="LA civic orgs", industry="Civic", region="CA", unlock_usd="5",
                royalty_usd={"connected": "0.25", "pitched": "1"}, guarantee_tier="phone_verified",
                rules={"window_days": 30}, consent_note="Public records", sms_consent=False)
    args.update(kw)
    return selling.publish(db, owner, **args)


def test_publish_takes_only_leads_we_can_stand_behind(db, store):
    owner, _cid, list_id, good, (unworked, blocked, bought) = setup_list(db)
    found = selling.preview(db, db.get_prospect_saved_list(list_id)["criteria"], "phone_verified")
    assert found["matched"] == 6 and len(found["eligible"]) == 5  # do-not-sell and bought leads never match
    package_id, problem = publish(db, owner, list_id)
    assert package_id and not problem
    published = {r["prospect_id"] for r in db.conn.execute("SELECT prospect_id FROM published_leads").fetchall()}
    assert published == set(good)
    assert publish(db, owner, list_id, guarantee_tier="pitched")[1].startswith("None of this list")
    assert publish(db, owner, list_id, unlock_usd="0")[1] == "Set an unlock price"


def test_catalog_is_readable_by_our_own_buyer(db, store):
    owner, _cid, list_id, _good, _ = setup_list(db)
    publish(db, owner, list_id)
    [entry] = client_for().get("/x402/packages").json()["packages"]
    package = lead_packages.Package.from_catalog("http://127.0.0.1/x402", entry)
    assert package and package.guaranteed and package.guarantee_tier == "phone_verified"
    assert package.unlock_price_atomic == 5_000_000 and package.royalty_for("connected") == 250_000
    assert package.lead_count == 5 and package.pay_to == PAY_TO and package.guarantee["rules"]["window_days"] == 30


def test_store_is_closed_until_configured(db, monkeypatch):
    monkeypatch.delenv("AGENCY_OS_SELL", raising=False)
    assert client_for().get("/x402/packages").status_code == 404


def buy(client, slug):
    unpaid = client.get(f"/x402/packages/{slug}/leads")
    assert unpaid.status_code == 402
    quote = json.loads(base64.b64decode(unpaid.headers["PAYMENT-REQUIRED"]))
    assert quote["accepts"][0]["amount"] == "5000000"
    paid = client.get(f"/x402/packages/{slug}/leads", headers={"PAYMENT-SIGNATURE": "fake-signature"})
    assert paid.status_code == 200, paid.text
    assert json.loads(base64.b64decode(paid.headers["PAYMENT-RESPONSE"]))["success"]
    return paid.json()


def slug_of(db):
    return db.conn.execute("SELECT slug FROM published_packages ORDER BY id DESC LIMIT 1").fetchone()["slug"]


def test_sale_royalty_and_buyer_checks(db, store, monkeypatch):
    owner, _cid, list_id, good, _ = setup_list(db)
    publish(db, owner, list_id)
    client, slug = client_for(), slug_of(db)
    assert client.get(f"/x402/packages/{slug}/leads", headers={"PAYMENT-SIGNATURE": "forged"}).status_code == 402
    sale = buy(client, slug)
    assert len(sale["leads"]) == 5 and sale["claim_token"]
    lead = sale["leads"][0]
    assert lead["contact_email"] and lead["tier"] == "connected"
    assert lead["contact_history"][0]["outcome"] == "completed"
    assert db.conn.execute("SELECT kind, amount_atomic FROM spend").fetchone()["kind"] == "unlock_in"

    url = f"/x402/packages/{slug}/contacts"
    body = {"lead_id": lead["lead_id"], "campaign_ref": "c1"}
    quote = client.post(url, json=body)
    assert quote.status_code == 402
    assert json.loads(base64.b64decode(quote.headers["PAYMENT-REQUIRED"]))["accepts"][0]["amount"] == "250000"
    assert client.post(url, json=body, headers={"PAYMENT-SIGNATURE": "fake-signature"}).status_code == 200
    assert client.post(url, json=body, headers={"PAYMENT-SIGNATURE": "fake-signature"}).status_code == 409
    store.payer = "0x" + "99" * 20  # someone who didn't buy this lead
    assert client.post(url, json=body, headers={"PAYMENT-SIGNATURE": "fake-signature"}).status_code == 403
    assert store.settled == [5_000_000, 250_000]  # never settled the refused ones


def test_do_not_sell_after_publishing(db, store):
    owner, _cid, list_id, good, _ = setup_list(db)
    publish(db, owner, list_id)
    selling.set_do_not_sell(db, owner, good[0], True)
    sale = buy(client_for(), slug_of(db))
    assert len(sale["leads"]) == 4


def claim(client, slug, token, lead_ids, shortfall=None):
    body = {"claim_token": token, "failed": [{"lead_id": l, "check": "contact", "evidence": ["Disconnected"]}
                                             for l in lead_ids]}
    if shortfall is not None:
        body["shortfall"] = shortfall
    return client.post(f"/x402/packages/{slug}/claims", json=body).json()


def test_claims_replace_dispute_and_review(db, store):
    owner, campaign_id, list_id, good, _ = setup_list(db)
    publish(db, owner, list_id)
    client, slug = client_for(), slug_of(db)
    sale = buy(client, slug)
    leads = [l["lead_id"] for l in sale["leads"]]

    assert client.post(f"/x402/packages/{slug}/claims", json={"claim_token": "stolen"}).status_code == 403
    # 1 failure in 5 breaks 90% (needs all 5), so 1 replacement is owed; a new prospect fills it.
    replacement_pid = org(db, campaign_id, 20)
    replaced = claim(client, slug, sale["claim_token"], [leads[0]])
    assert replaced["remedy"] == "replacement" and len(replaced["leads"]) == 1
    assert replaced["leads"][0]["name"] == "Org 20"
    assert claim(client, slug, sale["claim_token"], [leads[0]])["remedy"] == "disputed"  # already claimed

    # A lead we've reached since the sale isn't dead.
    oid = db.conn.execute("SELECT id FROM outreach WHERE prospect_id = ?", (good[1],)).fetchone()["id"]
    db.log_call(CallLog(outreach_id=oid, campaign_id=campaign_id, prospect_id=good[1], outcome="completed"))
    disputed = claim(client, slug, sale["claim_token"], [leads[1]])
    assert disputed["remedy"] == "disputed" and "reached" in disputed["reason"]

    # Nothing left to replace with: an owner reviews and refunds by hand.
    review = claim(client, slug, sale["claim_token"], [leads[2]])
    assert review["remedy"] == "review"
    claim_id = db.conn.execute("SELECT id FROM incoming_claims WHERE status = 'review'").fetchone()["id"]
    assert selling.record_refund(db, owner, claim_id, "1", "nope") == "Enter the refund's 0x transaction hash"
    assert selling.record_refund(db, owner, claim_id, "1", "0x" + "ab" * 32) == ""
    assert db.conn.execute("SELECT status FROM incoming_claims WHERE id = ?", (claim_id,)).fetchone()["status"] == "refunded"
    assert db.conn.execute("SELECT 1 FROM spend WHERE kind = 'refund_out'").fetchone()
    assert replacement_pid


def test_claims_after_the_window_are_disputed(db, store):
    owner, _cid, list_id, _good, _ = setup_list(db)
    publish(db, owner, list_id, rules={"window_days": 7, "claim_days": 0})
    client, slug = client_for(), slug_of(db)
    sale = buy(client, slug)
    db.conn.execute("UPDATE package_sales SET created_at = CURRENT_TIMESTAMP - INTERVAL '8 days'")
    result = claim(client, slug, sale["claim_token"], [sale["leads"][0]["lead_id"]])
    assert result["remedy"] == "disputed" and "closed" in result["reason"]


def test_selling_page_publishes_in_two_steps(db, store):
    owner, _cid, list_id, _good, _ = setup_list(db)
    page = client_for("owner@x.com")
    assert "Publish a saved list" in page.get("/admin/selling").text
    form = {"saved_list_id": str(list_id), "guarantee_tier": "phone_verified", "title": "LA orgs",
            "unlock_usd": "5", "royalty_connected": "0.25"}
    checked = page.post("/admin/selling/publish", data={**form, "action": "preview"})
    assert "5 of 6 prospects" in checked.text and db.conn.execute("SELECT 1 FROM published_packages").fetchone() is None
    unconfirmed = page.post("/admin/selling/publish", data={**form, "action": "publish"})
    assert "Tick the box" in unconfirmed.text
    done = page.post("/admin/selling/publish", data={**form, "action": "publish", "confirm": "yes"})
    assert done.status_code == 303 and "LA orgs" in page.get("/admin/selling").text
    make_user(db, "rep@x.com", "Sales Rep")
    assert client_for("rep@x.com").get("/admin/selling").status_code == 403


def test_buying_from_ourselves_end_to_end(db, store, monkeypatch):
    """Our buyer against our own seller endpoints: the dev-provider loop."""
    from fastapi.testclient import TestClient

    import web.app as webapp
    from core import payments
    from tests.fake_x402_provider import FakePayer

    owner, _cid, list_id, _good, _ = setup_list(db)
    publish(db, owner, list_id)
    provider = "http://127.0.0.1/x402"
    monkeypatch.setenv("AGENCY_OS_X402", "on")
    monkeypatch.setenv("AGENCY_OS_LEAD_PROVIDERS", provider)
    http = TestClient(webapp.app, base_url="http://127.0.0.1")
    package, error = lead_packages.find_package(provider, slug_of(db), http)
    assert package, error
    db.set_spend_allowance(owner.id, payments.usd_to_atomic(100), actor=None)
    buyer_campaign = type("C", (), {"lead_packages": {"enabled": True, "network": "base-sepolia", "max_unlock_usd": 10,
                                                      "max_royalty_per_contact_usd": 1, "monthly_budget_usd": 100},
                                    "db_name": "buyer"})()
    buyer_id = db.upsert_campaign("buyer", "x")
    result = lead_packages.unlock(db, buyer_campaign, buyer_id, owner, package, http=http, payer=FakePayer())
    assert result.ok and result.duplicates == 5  # same database: every lead is already ours
    assert db.get_lead_package(result.lead_package_id)["claim_token"]
    assert store.settled == [5_000_000]


def test_real_x402_sdk_payment_path(db, store, monkeypatch):
    """The SDK's own signer (buyer) and resource server (seller), with a stand-in facilitator."""
    pytest.importorskip("x402")
    eth_account = pytest.importorskip("eth_account")
    from x402.schemas import SettleResponse, SupportedKind, SupportedResponse, VerifyResponse

    from core import payments

    class Facilitator:
        def __init__(self):
            self.verified = []

        def get_supported(self):
            return SupportedResponse(kinds=[SupportedKind(x402_version=2, scheme="exact", network=BASE_SEPOLIA)])

        def verify(self, payload, requirements):
            self.verified.append((payload, requirements))
            return VerifyResponse(is_valid=True, payer=payload.payload["authorization"]["from"])

        def settle(self, payload, requirements):
            return SettleResponse(success=True, transaction="0x" + "77" * 32, network=BASE_SEPOLIA,
                                  payer=payload.payload["authorization"]["from"])

    facilitator = Facilitator()
    gate = selling.X402Gate(facilitator)
    monkeypatch.setattr(selling, "_gate", gate)
    owner, _cid, list_id, _good, _ = setup_list(db)
    publish(db, owner, list_id)
    client, slug = client_for(), slug_of(db)

    unpaid = client.get(f"/x402/packages/{slug}/leads")
    assert unpaid.status_code == 402
    policy = payments.SpendPolicy.from_config({"enabled": True, "network": "base-sepolia", "max_unlock_usd": 10})
    quote = payments.parse_quote(unpaid, policy)
    assert quote.amount_atomic == 5_000_000 and quote.pay_to.lower() == PAY_TO

    payer = payments.CdpPayer()
    payer._account = eth_account.Account.create()
    paid = client.get(f"/x402/packages/{slug}/leads", headers=payer.payment_headers(quote))
    assert paid.status_code == 200, paid.text
    settlement = payments.parse_settlement(paid)
    assert settlement["success"] and settlement["transaction"] == "0x" + "77" * 32
    sale = db.conn.execute("SELECT payer FROM package_sales").fetchone()
    assert sale["payer"] == payer._account.address.lower() and len(facilitator.verified) == 1
