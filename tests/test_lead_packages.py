"""
x402 lead packages: payment policy, the spend ledger, unlocking, royalties.

The provider and the wallet are fakes (tests/fake_x402_provider.py); nothing
touches the network or real money. Tests marked with the `db` fixture need
TEST_DATABASE_URL (see conftest.py).

Run: python -m pytest tests/test_lead_packages.py
"""

import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import access, lead_packages, payments  # noqa: E402
from core.campaign import CadenceStep, CampaignConfig  # noqa: E402
from core.models import Prospect, SendResult  # noqa: E402
from core.payments import BASE_SEPOLIA, USDC, Expected, SpendPolicy  # noqa: E402
from core.pipeline import Pipeline  # noqa: E402
from tests.fake_x402_provider import BASE, PAY_TO, FakePayer, FakeProvider, b64  # noqa: E402
from tests.test_access import client_for, db, make_user  # noqa: E402,F401

POLICY = {"enabled": True, "network": "base-sepolia", "max_unlock_usd": 10,
          "max_royalty_per_contact_usd": 0.5, "monthly_budget_usd": 100}


@pytest.fixture
def x402_env(monkeypatch):
    monkeypatch.setenv("AGENCY_OS_X402", "on")
    monkeypatch.setenv("AGENCY_OS_LEAD_PROVIDERS", BASE)
    monkeypatch.delenv("AGENCY_OS_X402_ALLOW_MAINNET", raising=False)
    lead_packages._catalog_cache.clear()


@pytest.fixture
def provider():
    return FakeProvider()


def campaign(**policy):
    return SimpleNamespace(lead_packages={**POLICY, **policy}, db_name="lp-test")


def buyer(db, email="buyer@x.com", allowance_usd=100):
    user_id = make_user(db, email, access.OWNER_ROLE)
    db.set_spend_allowance(user_id, payments.usd_to_atomic(allowance_usd), actor=None)
    return db.load_current_user(user_id)


def package(provider, package_id="p1"):
    found, error = lead_packages.find_package(BASE, package_id, provider.client())
    assert found, error
    return found


def spend_rows(db):
    return [dict(r) for r in db.conn.execute("SELECT * FROM spend ORDER BY id").fetchall()]


# ── Pure checks (no database) ──────────────────────────────────────────


def test_provider_urls_must_be_https_or_localhost():
    assert lead_packages.normalize_provider("https://leads.example/api/") == "https://leads.example/api"
    assert lead_packages.normalize_provider("http://localhost:9000") == "http://localhost:9000"
    for bad in ("http://leads.example", "ftp://x", "https://user:pw@leads.example",
                "https://leads.example/?a=1", "javascript:alert(1)", ""):
        assert lead_packages.normalize_provider(bad) is None


def test_only_allowlisted_providers(monkeypatch):
    monkeypatch.setenv("AGENCY_OS_LEAD_PROVIDERS", "https://a.example, http://evil.example, https://a.example")
    assert lead_packages.configured_providers() == ["https://a.example"]
    packages, error = lead_packages.fetch_catalog("https://not-listed.example")
    assert packages == [] and "allowlist" in error


def test_catalog_entries_are_validated(provider):
    good = provider.packages["p1"]
    assert lead_packages.Package.from_catalog(BASE, good).unlock_price_atomic == 5_000_000
    for broken in ({"id": "../etc"}, {"pay_to": "not-an-address"}, {"network": "eip155:1"},
                   {"unlock_price_atomic": "-5"}, {"royalty_atomic": "lots"}):
        assert lead_packages.Package.from_catalog(BASE, {**good, **broken}) is None


def test_leads_are_whitelisted_and_cleaned():
    lead = lead_packages.parse_lead({
        "lead_id": "L1", "name": "  Org  ", "state": "california", "contact_email": "Bad Email",
        "website_url": "javascript:alert(1)", "contact_phone": "555-0100<script>", "metadata": {"x": 1},
        "id": 5,
    })
    assert lead["name"] == "Org" and lead["state"] == "CA"
    assert lead["contact_email"] is None and lead["website_url"] is None and lead["contact_phone"] is None
    assert "metadata" not in lead and "id" not in lead
    assert lead_packages.parse_lead({"name": "No id"}) is None


def quote_response(**accept):
    body = {"x402Version": 2, "accepts": [{
        "scheme": "exact", "network": BASE_SEPOLIA, "asset": USDC[BASE_SEPOLIA],
        "amount": "1000", "payTo": PAY_TO, "maxTimeoutSeconds": 60, **accept}]}
    return httpx.Response(402, headers={"PAYMENT-REQUIRED": b64(body)})


def test_quote_must_be_usdc_on_the_campaign_network():
    policy = SpendPolicy.from_config(POLICY)
    assert payments.parse_quote(quote_response(), policy).amount_atomic == 1000
    for bad in ({"network": "eip155:8453"}, {"asset": "0x" + "11" * 20}, {"scheme": "upto"}, {"amount": "0"},
                {"maxTimeoutSeconds": None}, {"maxTimeoutSeconds": 0}):
        with pytest.raises(ValueError):
            payments.parse_quote(quote_response(**bad), policy)
    with pytest.raises(ValueError):
        payments.parse_quote(httpx.Response(402), policy)


def test_quote_checked_against_catalog_and_caps():
    policy = SpendPolicy.from_config(POLICY)  # unlock cap $10
    quote = payments.parse_quote(quote_response(amount="6000000"), policy)
    assert payments.quote_problem(quote, Expected(PAY_TO, 6_000_000), policy, "unlock") is None
    assert "different address" in payments.quote_problem(quote, Expected("0x" + "cd" * 20, 6_000_000), policy, "unlock")
    assert "higher than the catalog" in payments.quote_problem(quote, Expected(PAY_TO, 5_000_000), policy, "unlock")
    assert "royalty cap" in payments.quote_problem(quote, Expected(PAY_TO, 6_000_000), policy, "royalty")


def test_policy_is_off_by_default_and_mainnet_is_gated(monkeypatch):
    monkeypatch.delenv("AGENCY_OS_X402", raising=False)
    assert "turned off" in SpendPolicy.from_config(POLICY).problem()
    monkeypatch.setenv("AGENCY_OS_X402", "on")
    assert "not enabled" in SpendPolicy.from_config({}).problem()
    assert SpendPolicy.from_config(POLICY).problem() is None
    assert "MAINNET" in SpendPolicy.from_config({**POLICY, "network": "base"}).problem()
    assert SpendPolicy.from_config({**POLICY, "network": "solana"}).network == "base-sepolia"


# ── Unlocking (database) ───────────────────────────────────────────────


def test_unlock_pays_once_and_imports_leads(db, x402_env, provider):
    user = buyer(db)
    campaign_id = db.upsert_campaign("lp-test", "x")
    payer = FakePayer()
    result = lead_packages.unlock(db, campaign(), campaign_id, user, package(provider),
                                  http=provider.client(), payer=payer)
    assert result.ok and result.imported == 3, result.message
    assert provider.paid_calls == [("unlock", "p1")]
    [row] = spend_rows(db)
    assert row["status"] == "settled" and row["amount_atomic"] == 5_000_000 and row["tx_hash"]
    assert row["lead_package_id"] == result.lead_package_id and row["user_id"] == user.id

    outreach = db.conn.execute("SELECT * FROM outreach WHERE campaign_id = ?", (campaign_id,)).fetchall()
    assert len(outreach) == 3 and all(o["contact_email"] for o in outreach)
    prospect = db.get_prospect(outreach[0]["prospect_id"])
    assert prospect.metadata["lead_package"]["lead_package_id"] == result.lead_package_id

    again = lead_packages.unlock(db, campaign(), campaign_id, user, package(provider),
                                 http=provider.client(), payer=payer)
    assert again.ok and "Already unlocked" in again.message
    assert len(provider.paid_calls) == 1 and len(spend_rows(db)) == 1


def test_existing_prospects_are_never_overwritten(db, x402_env, provider):
    user = buyer(db)
    campaign_id = db.upsert_campaign("lp-test", "x")
    mine = db.upsert_prospect(Prospect(name="Mine", ein="99-0000001", source="irs_bmf",
                                       metadata={"note": "ours"}))
    result = lead_packages.unlock(db, campaign(), campaign_id, user, package(provider),
                                  http=provider.client(), payer=FakePayer())
    assert result.imported == 2 and result.duplicates == 1
    kept = db.get_prospect(mine)
    assert kept.name == "Mine" and kept.metadata == {"note": "ours"}


@pytest.mark.parametrize("change, reason", [
    ({"quote_amount": {"unlock:p1": 6_000_000}}, "higher than the catalog"),
    ({"quote_pay_to": "0x" + "cd" * 20}, "different address"),
])
def test_bait_and_switch_quotes_are_refused(db, x402_env, provider, change, reason):
    for k, v in change.items():
        setattr(provider, k, v)
    user = buyer(db)
    campaign_id = db.upsert_campaign("lp-test", "x")
    payer = FakePayer()
    result = lead_packages.unlock(db, campaign(), campaign_id, user, package(provider),
                                  http=provider.client(), payer=payer)
    assert not result.ok and reason in result.message
    assert payer.quotes == [] and provider.paid_calls == []
    assert [r["status"] for r in spend_rows(db)] == ["refused"]


@pytest.mark.parametrize("policy, allowance, reason", [
    ({"max_unlock_usd": 1}, 100, "unlock cap"),
    ({"monthly_budget_usd": 4}, 100, "monthly budget"),
    ({}, 0, "allowance"),
])
def test_caps_budget_and_allowance(db, x402_env, provider, policy, allowance, reason):
    user = buyer(db, allowance_usd=allowance)
    campaign_id = db.upsert_campaign("lp-test", "x")
    result = lead_packages.unlock(db, campaign(**policy), campaign_id, user, package(provider),
                                  http=provider.client(), payer=FakePayer())
    assert not result.ok and reason in result.message
    assert provider.paid_calls == []


def test_disabled_campaign_or_kill_switch_never_pays(db, x402_env, provider, monkeypatch):
    user = buyer(db)
    campaign_id = db.upsert_campaign("lp-test", "x")
    payer = FakePayer()
    off = lead_packages.unlock(db, campaign(enabled=False), campaign_id, user, package(provider),
                               http=provider.client(), payer=payer)
    monkeypatch.setenv("AGENCY_OS_X402", "off")
    killed = lead_packages.unlock(db, campaign(), campaign_id, user, package(provider),
                                  http=provider.client(), payer=payer)
    assert not off.ok and not killed.ok
    assert payer.quotes == [] and provider.paid_calls == []


def test_failed_settlement_releases_the_budget(db, x402_env, provider):
    provider.settle_success = False
    user = buyer(db)
    campaign_id = db.upsert_campaign("lp-test", "x")
    result = lead_packages.unlock(db, campaign(), campaign_id, user, package(provider),
                                  http=provider.client(), payer=FakePayer())
    assert not result.ok and "insufficient_funds" in result.message
    assert [r["status"] for r in spend_rows(db)] == ["failed"]
    assert db.campaign_spend(campaign_id)["month_atomic"] == 0
    assert db.find_lead_package(BASE, "p1", campaign_id) is None


def test_concurrent_unlocks_cannot_overspend(db, x402_env, provider):
    provider.add_package("p2", 6_000_000)
    provider.add_package("p3", 6_000_000)
    user = buyer(db)
    campaign_id = db.upsert_campaign("lp-test", "x")
    policy = campaign(monthly_budget_usd=10)  # room for one $6 package, not two
    packages = [package(provider, "p2"), package(provider, "p3")]
    results = [None, None]

    def run(i):
        results[i] = lead_packages.unlock(db, policy, campaign_id, user, packages[i],
                                          http=provider.client(), payer=FakePayer())

    threads = [threading.Thread(target=run, args=(i,)) for i in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(r.ok for r in results) == [False, True]
    assert len(provider.paid_calls) == 1
    assert db.campaign_spend(campaign_id)["month_atomic"] == 6_000_000


# ── Royalties ──────────────────────────────────────────────────────────


def unlocked_lead(db, provider, **policy):
    user = buyer(db)
    campaign_id = db.upsert_campaign("lp-test", "x")
    result = lead_packages.unlock(db, campaign(**policy), campaign_id, user, package(provider),
                                  http=provider.client(), payer=FakePayer())
    assert result.ok, result.message
    row = db.conn.execute("SELECT prospect_id FROM outreach WHERE campaign_id = ? ORDER BY id LIMIT 1",
                          (campaign_id,)).fetchone()
    return campaign_id, db.get_prospect(row["prospect_id"])


def test_first_touch_pays_the_royalty_once(db, x402_env, provider):
    campaign_id, prospect = unlocked_lead(db, provider)
    gate = lead_packages.gate_contact(db, campaign(), campaign_id, prospect, 0,
                                      http=provider.client(), payer=FakePayer())
    assert gate.send and gate.payment.status == "settled"
    assert provider.paid_calls[-1] == ("royalty", "L1")
    # Retried first touch (e.g. the send failed) and later touches don't pay again.
    for touch in (0, 1, 2):
        assert lead_packages.gate_contact(db, campaign(), campaign_id, prospect, touch,
                                          http=provider.client(), payer=FakePayer()).send
    assert [c[0] for c in provider.paid_calls].count("royalty") == 1
    assert not gate.sms_allowed  # the package has no SMS consent


def test_royalty_refused_means_no_contact(db, x402_env, provider):
    campaign_id, prospect = unlocked_lead(db, provider, max_royalty_per_contact_usd=0.01)
    gate = lead_packages.gate_contact(db, campaign(max_royalty_per_contact_usd=0.01), campaign_id,
                                      prospect, 0, http=provider.client(), payer=FakePayer())
    assert not gate.send and "royalty cap" in gate.reason


def test_dry_run_and_other_campaigns(db, x402_env, provider):
    campaign_id, prospect = unlocked_lead(db, provider)
    dry = lead_packages.gate_contact(db, campaign(), campaign_id, prospect, 0, dry_run=True,
                                     http=provider.client(), payer=FakePayer())
    assert dry.send and "Would pay" in dry.reason
    other = db.upsert_campaign("other", "x")
    assert not lead_packages.gate_contact(db, campaign(), other, prospect, 0).send
    assert [c[0] for c in provider.paid_calls] == ["unlock"]
    plain = Prospect(name="Not from a package", id=1)
    assert lead_packages.gate_contact(db, campaign(), campaign_id, plain, 0).send


class FakeChannel:
    key = "email_fake"

    def __init__(self):
        self.sent = []

    def is_configured(self):
        return True

    def send(self, recipient, subject, body, metadata):
        self.sent.append(recipient["email"])
        return SendResult(status="sent")


def enqueue_setup(db, provider, tmp_path, **policy):
    scripts = tmp_path / "scripts"
    scripts.mkdir(exist_ok=True)
    (scripts / "00_cold.yaml").write_text("key: cold\nsubject: Hi {{org_name}}\nbody: Hello\n")
    config = CampaignConfig(
        name="lp-test", product="none", prospect_sources=[], channels=["sms_fake", "email_fake"],
        cadence=[CadenceStep(touch=0, delay_days=3, script="00_cold", next_stage="contacted")],
        lead_packages={**POLICY, **policy}, config_dir=tmp_path,
    )
    unlocked_lead(db, provider, **policy)
    sms, email = FakeChannel(), FakeChannel()
    registry = SimpleNamespace(get_channel={"sms_fake": sms, "email_fake": email}.get,
                               get_scheduler=lambda key: None, get_product=lambda key: None)
    pipeline = Pipeline(db, registry)
    pipeline.http, pipeline.payer = provider.client(), FakePayer()
    return pipeline, config, sms, email


def royalties_paid(db):
    return sorted(r["amount_atomic"] for r in spend_rows(db) if r["kind"] == "royalty" and r["status"] == "settled")


def test_enqueue_pays_each_leads_tier_royalty_before_sending(db, x402_env, provider, tmp_path):
    pipeline, config, sms, email = enqueue_setup(db, provider, tmp_path, max_royalty_per_contact_usd=2)
    stats = pipeline.enqueue_outreach(config, limit=10)
    assert stats["sent"] == 3 and stats["royalty_blocked"] == 0
    assert sms.sent == [] and len(email.sent) == 3  # no SMS consent in this package
    assert royalties_paid(db) == [250_000, 250_000, 1_000_000]  # 2 phone_verified, 1 pitched


def test_enqueue_skips_a_lead_whose_royalty_is_over_the_cap(db, x402_env, provider, tmp_path):
    pipeline, config, _sms, email = enqueue_setup(db, provider, tmp_path)  # cap $0.50
    stats = pipeline.enqueue_outreach(config, limit=10)
    assert stats["sent"] == 2 and stats["royalty_blocked"] == 1
    assert "pat3@org3.example" not in email.sent  # the pitched lead ($1.00) was not contacted unpaid
    assert royalties_paid(db) == [250_000, 250_000]


# ── Web ────────────────────────────────────────────────────────────────


def test_unlock_route_needs_permission_and_confirmation(db, x402_env, provider, monkeypatch):
    import web.app as webapp

    monkeypatch.setattr(lead_packages, "make_http", lambda transport=None: provider.client())
    monkeypatch.setattr(payments, "_default_payer", FakePayer())
    monkeypatch.setattr(webapp, "get_campaigns", lambda: [CampaignConfig(
        name="lp-test", product="none", prospect_sources=[], channels=[], lead_packages=POLICY,
        config_dir=Path("/tmp"))])
    make_user(db, "viewer@x.com", "Viewer")
    form = {"provider": BASE, "package_id": "p1", "campaign": "lp-test", "confirm": "yes"}
    assert client_for("viewer@x.com").post("/lead-packages/unlock", data=form).status_code == 403

    buyer(db, "owner@x.com")
    owner = client_for("owner@x.com")
    unconfirmed = owner.post("/lead-packages/unlock", data={**form, "confirm": ""})
    assert "approve" in unconfirmed.headers["location"] and provider.paid_calls == []
    assert owner.get("/lead-packages").status_code == 200

    done = owner.post("/lead-packages/unlock", data=form)
    assert "Unlocked" in httpx.URL(done.headers["location"]).params["msg"]
    assert provider.paid_calls == [("unlock", "p1")]
    page = owner.get("/lead-packages").text
    assert "LA civic nonprofits" in page and "sepolia.basescan.org/tx/0x" in page


# ── Real signer (needs requirements-payments.txt; no wallet or network) ──


def test_cdp_payer_signs_exactly_the_quote_with_the_x402_sdk():
    pytest.importorskip("x402")
    eth_account = pytest.importorskip("eth_account")
    from x402.http.utils import decode_payment_signature_header

    policy = SpendPolicy.from_config(POLICY)
    quote = payments.parse_quote(quote_response(amount="250000"), policy)
    payer = payments.CdpPayer()
    payer._account = eth_account.Account.create()  # stands in for the CDP server account

    headers = payer.payment_headers(quote)
    payload = decode_payment_signature_header(headers["PAYMENT-SIGNATURE"])
    assert payload.accepted.pay_to == PAY_TO and payload.accepted.amount == "250000"
    assert payload.accepted.network == BASE_SEPOLIA
    auth = payload.payload["authorization"]
    assert auth["to"].lower() == PAY_TO.lower() and int(auth["value"]) == 250000
    assert auth["from"] == payer._account.address and payload.payload["signature"].startswith("0x")


def test_campaign_editor_saves_policy_and_never_pays(db, x402_env, provider):
    import yaml

    import web.app as webapp

    buyer(db, "owner@x.com")
    owner = client_for("owner@x.com")
    config = webapp.get_campaigns()[0]
    url = f"/admin/campaigns/{config.db_name}"
    assert "Lead Packages" in owner.get(url).text

    owner.post(url, data={"lp_present": "1", "lp_enabled": "1", "lp_network": "base",
                          "lp_max_unlock_usd": "25", "lp_max_royalty_usd": "-3",
                          "lp_monthly_budget_usd": "lots"})
    saved = yaml.safe_load((config.config_dir / "campaign.yaml").read_text())["lead_packages"]
    assert saved == {"enabled": True, "network": "base", "max_unlock_usd": 25.0,
                     "max_royalty_per_contact_usd": 0.0, "monthly_budget_usd": 0.0}

    # A post without the section (older form, other tools) leaves spending settings alone.
    owner.post(url, data={"sender_name": "Joshua"})
    assert yaml.safe_load((config.config_dir / "campaign.yaml").read_text())["lead_packages"] == saved
    owner.post(url, data={"lp_present": "1", "lp_network": "solana"})
    off = yaml.safe_load((config.config_dir / "campaign.yaml").read_text())["lead_packages"]
    assert off["enabled"] is False and off["network"] == "base-sepolia"
    assert provider.paid_calls == [] and spend_rows(db) == []
