"""
The 90% guarantee: scoring evidence, the shortfall, claims and their remedies,
the optional AI review, and refreshing a bounced email.

The provider, wallet and AI are fakes; nothing touches the network.

Run: TEST_DATABASE_URL=postgresql://localhost/agency_os_test python -m pytest tests/test_verify.py
"""

import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import claims, lead_packages, verify  # noqa: E402
from core.models import CallLog, EnrichmentResult, SendResult  # noqa: E402
from core.verify import score_lead, summarize  # noqa: E402
from tests.fake_x402_provider import BASE, FakePayer  # noqa: E402
from tests.test_access import client_for, db, make_user  # noqa: E402,F401
from tests.test_lead_packages import (  # noqa: E402,F401
    buyer, campaign, package, provider, spend_rows, x402_env,
)


def score(calls=(), events=(), emails=(), seller="phone_verified", promised="phone_verified", **kw):
    return score_lead(calls=[c if isinstance(c, dict) else {"outcome": c} for c in calls],
                      events=[e if isinstance(e, dict) else {"kind": e} for e in events],
                      email_statuses=list(emails), seller_tier=seller, promised_tier=promised, **kw)


# ── Scoring (pure) ─────────────────────────────────────────────────────


@pytest.mark.parametrize("calls, status", [
    (["disconnected"], "failed"),
    (["fax_tone"], "failed"),
    (["wrong_number"], "verified"),                       # one misdial can't fail a lead
    (["wrong_number", "wrong_number"], "failed"),
    ([{"outcome": "wrong_number", "called_by": "Ana"}, {"outcome": "wrong_number", "called_by": "Ben"}], "failed"),
    (["no_answer"] * 6, "verified"),                       # weak on its own (0.5)
    (["disconnected", "completed"], "verified"),           # reaching the org outweighs it
    (["voicemail", "hung_up"], "verified"),
])
def test_call_dispositions(calls, status):
    assert score(calls).status == status


def test_wrong_number_threshold_is_explained():
    once = score(["wrong_number"])
    assert once.score == 0 and "counts at 2 reports" in once.signals[0].label


def test_email_mail_and_enricher_signals_combine():
    assert score(events=["email_bounced"]).score == 0.5
    assert score(events=["email_bounced", "enricher_mismatch"]).status == "failed"
    assert score(events=["enricher_mismatch"]).score == 0.25
    assert score(emails=["bounced"], events=["mail_returned"]).status == "failed"
    assert score(events=["email_bounced", "enricher_match"]).score == 0.25
    assert score(["no_answer"] * 6, events=["email_bounced"]).status == "failed"


def test_bounce_only_counts_for_the_packages_email():
    other = {"kind": "email_bounced", "value": "new@org.example"}
    assert score(events=[other], package_email="pat@org.example").score == 0
    assert score(events=[{**other, "value": "pat@org.example"}], package_email="pat@org.example").score == 0.5


def test_tier_below_promise_fails_even_unworked():
    v = score(seller="mailed", promised="phone_verified")
    assert v.status == "failed" and v.failed_check == "tier"


def test_unworked_until_we_do_something():
    assert score().status == "unworked"
    assert score(emails=["sent"]).status == "verified"


def test_ai_is_one_weighted_signal():
    assert score(ai_verdict="not_real", ai_reason="Closed in 2025").status == "verified"  # never alone
    assert score(events=["email_bounced"], ai_verdict="not_real").status == "failed"
    assert score(["disconnected"], ai_verdict="real").score == 0.5


@pytest.mark.parametrize("n, failed, shortfall", [(3, 0, 0), (3, 1, 1), (10, 1, 0), (10, 2, 1), (100, 15, 5)])
def test_shortfall_is_what_90_percent_needs(n, failed, shortfall):
    checks = [{"status": "failed" if i < failed else "verified", "replacement": 0, "claim_id": None}
              for i in range(n)]
    assert summarize({}, checks, True)["shortfall"] == shortfall


# ── Database: verdicts, gate, claims ───────────────────────────────────


def unlocked(db, provider):
    user = buyer(db)
    campaign_id = db.upsert_campaign("lp-test", "x")
    result = lead_packages.unlock(db, campaign(), campaign_id, user, package(provider),
                                  http=provider.client(), payer=FakePayer())
    assert result.ok, result.message
    rows = db.conn.execute("SELECT * FROM lead_checks WHERE lead_package_id = ? ORDER BY id",
                           (result.lead_package_id,)).fetchall()
    return user, campaign_id, result.lead_package_id, [dict(r) for r in rows]


def call(db, campaign_id, prospect_id, outcome, called_by="Rep", notes=""):
    outreach_id = db.conn.execute("SELECT id FROM outreach WHERE prospect_id = ? AND campaign_id = ?",
                                  (prospect_id, campaign_id)).fetchone()["id"]
    db.log_call(CallLog(outreach_id=outreach_id, campaign_id=campaign_id, prospect_id=prospect_id,
                        outcome=outcome, called_by=called_by, notes=notes))


def test_import_records_each_lead_for_the_guarantee(db, x402_env, provider):
    _user, campaign_id, lp_id, rows = unlocked(db, provider)
    assert [(r["lead_id"], r["seller_tier"]) for r in rows] == [
        ("L1", "phone_verified"), ("L2", "phone_verified"), ("L3", "pitched")]
    assert rows[0]["package_email"] == "pat1@org1.example" and rows[0]["status"] == "unworked"

    call(db, campaign_id, rows[0]["prospect_id"], "disconnected")
    call(db, campaign_id, rows[1]["prospect_id"], "completed")
    by_lead = {r["lead_id"]: r["status"] for r in verify.evaluate_package(db, lp_id)}
    assert by_lead == {"L1": "failed", "L2": "verified", "L3": "unworked"}


def test_failed_lead_is_not_contacted_or_paid(db, x402_env, provider):
    _user, campaign_id, _lp_id, rows = unlocked(db, provider)
    call(db, campaign_id, rows[0]["prospect_id"], "fax_tone")
    prospect = db.get_prospect(rows[0]["prospect_id"])
    gate = lead_packages.gate_contact(db, campaign(), campaign_id, prospect, 0,
                                      http=provider.client(), payer=FakePayer())
    assert not gate.send and "failed verification" in gate.reason
    assert [c[0] for c in provider.paid_calls] == ["unlock"]


def fail_first_lead(db, campaign_id, rows):
    call(db, campaign_id, rows[0]["prospect_id"], "disconnected")


def test_claim_with_replacement_leads(db, x402_env, provider):
    user, campaign_id, lp_id, rows = unlocked(db, provider)
    fail_first_lead(db, campaign_id, rows)
    status = claims.package_status(db, lp_id)
    assert status["summary"]["shortfall"] == 1 and status["claim_problem"] is None

    result = claims.file_claim(db, lp_id, user, http=provider.client())
    assert result.ok and "1 replacement leads" in result.message, result.message
    [sent] = provider.claims
    assert sent["shortfall"] == 1 and sent["failed"][0]["lead_id"] == "L1"
    assert sent["failed"][0]["check"] == "contact" and "disconnected" in sent["failed"][0]["evidence"][0]
    assert sent["expected_refund_atomic"] == str(5_000_000 * 1 // 3)
    after = claims.package_status(db, lp_id)
    assert after["summary"]["replacements"] == 1  # capped at the shortfall
    assert after["summary"]["claimable"] == 0 and "already cover" in after["claim_problem"]
    assert after["claims"][0]["status"] == "replaced"
    assert not claims.file_claim(db, lp_id, user, http=provider.client()).ok
    assert len(provider.claims) == 1


def test_claim_with_refund_is_pending_until_confirmed(db, x402_env, provider):
    user, campaign_id, lp_id, rows = unlocked(db, provider)
    fail_first_lead(db, campaign_id, rows)
    tx = "0x" + "ab" * 32
    provider.claim_response = {"remedy": "refund", "refund_tx": tx, "refund_atomic": "1666666"}
    result = claims.file_claim(db, lp_id, user, http=provider.client())
    assert result.ok and "Confirm" in result.message
    [refund] = [r for r in spend_rows(db) if r["kind"] == "refund_in"]
    assert refund["status"] == "pending" and refund["tx_hash"] == tx
    spent = db.campaign_spend(campaign_id)
    assert spent["month_atomic"] == 5_000_000  # a refund never counts as spending
    assert db.resolve_spend(refund["id"], "settled", tx, actor=None)
    assert db.campaign_spend(campaign_id)["total_atomic"] == 5_000_000 - 1_666_666


def test_bad_refund_and_disputes(db, x402_env, provider):
    user, campaign_id, lp_id, rows = unlocked(db, provider)
    fail_first_lead(db, campaign_id, rows)
    provider.claim_response = {"remedy": "refund", "refund_tx": "nope", "refund_atomic": "5"}
    assert not claims.file_claim(db, lp_id, user, http=provider.client()).ok
    # A malformed answer releases the leads, so the claim can be filed again.
    provider.claim_response = {"remedy": "disputed", "reason": "We reached them on 2026-08-09"}
    result = claims.file_claim(db, lp_id, user, http=provider.client())
    assert not result.ok and "2026-08-09" in result.message
    statuses = [c["status"] for c in claims.package_status(db, lp_id)["claims"]]
    assert statuses == ["disputed", "error"]


def test_unreachable_provider_releases_the_leads(db, x402_env, provider):
    import httpx

    user, campaign_id, lp_id, rows = unlocked(db, provider)
    fail_first_lead(db, campaign_id, rows)

    def down(request):
        raise httpx.ConnectError("down", request=request)

    assert not claims.file_claim(db, lp_id, user, http=httpx.Client(transport=httpx.MockTransport(down))).ok
    assert claims.package_status(db, lp_id)["claim_problem"] is None
    assert claims.file_claim(db, lp_id, user, http=provider.client()).ok


def test_claims_only_inside_the_window(db, x402_env, provider):
    user, campaign_id, lp_id, rows = unlocked(db, provider)
    fail_first_lead(db, campaign_id, rows)
    db.conn.execute("UPDATE lead_packages SET unlocked_at = CURRENT_TIMESTAMP - INTERVAL '31 days' WHERE id = ?",
                    (lp_id,))
    status = claims.package_status(db, lp_id)
    assert not status["summary"]["window_open"] and "window has closed" in status["claim_problem"]
    assert not claims.file_claim(db, lp_id, user, http=provider.client()).ok and provider.claims == []


def test_two_people_filing_at_once_make_one_claim(db, x402_env, provider):
    user, campaign_id, lp_id, rows = unlocked(db, provider)
    fail_first_lead(db, campaign_id, rows)
    from core.db import Database

    barrier = threading.Barrier(2)
    results = []

    def file():
        own = Database(db.url)
        barrier.wait()
        results.append(claims.file_claim(own, lp_id, user, http=provider.client()))

    threads = [threading.Thread(target=file) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(r.ok for r in results) == [False, True]
    assert len(provider.claims) == 1


class FakeReviewer:
    def __init__(self, verdict="not_real"):
        self.verdict, self.calls = verdict, []

    def is_configured(self):
        return True

    def review(self, lead):
        self.calls.append(lead)
        return {"verdict": self.verdict, "reason": "Receptionist said they closed"}


def test_ai_review_is_cached_until_evidence_changes(db, x402_env, provider):
    _user, campaign_id, lp_id, rows = unlocked(db, provider)
    pid = rows[0]["prospect_id"]
    call(db, campaign_id, pid, "no_answer", notes="Recording says the office closed in 2025")
    reviewer = FakeReviewer()
    verify.evaluate_package(db, lp_id, reviewer=reviewer)
    verify.evaluate_package(db, lp_id, reviewer=reviewer)
    assert len(reviewer.calls) == 1 and "closed in 2025" in reviewer.calls[0]["calls"][0]["notes"]
    check = verify.lead_verdict(db, pid)  # no reviewer: keeps the cached verdict
    assert check["ai_verdict"] == "not_real" and check["status"] == "verified"

    db.conn.execute("INSERT INTO contact_events (prospect_id, kind, value) VALUES (?, 'email_bounced', ?)",
                    (pid, "pat1@org1.example"))
    verify.evaluate_package(db, lp_id, reviewer=reviewer)
    assert len(reviewer.calls) == 2  # new evidence, new review
    assert verify.lead_verdict(db, pid)["status"] == "failed"  # bounce 0.5 + AI 0.5


def test_ai_review_is_off_by_default(monkeypatch):
    monkeypatch.delenv("AGENCY_OS_AI_REVIEW", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    assert not verify.LLMReviewer().is_configured()  # needs the review switch too
    monkeypatch.setenv("AGENCY_OS_AI_REVIEW", "on")
    assert verify.LLMReviewer().is_configured()
    monkeypatch.setenv("AGENCY_OS_LLM", "off")
    assert not verify.LLMReviewer().is_configured()


class FakeEnricher:
    def __init__(self, email):
        self.email = email

    def is_configured(self):
        return True

    def enrich(self, prospect):
        return EnrichmentResult(contact_email=self.email)


def refresh(db, rows, campaign_id, email):
    registry = SimpleNamespace(get_enricher=lambda key: FakeEnricher(email))
    config = SimpleNamespace(enrichers=["fake"])
    pid = rows[0]["prospect_id"]
    outreach = dict(db.conn.execute("SELECT * FROM outreach WHERE prospect_id = ?", (pid,)).fetchone())
    return verify.refresh_email(db, registry, config, db.get_prospect(pid), outreach)


def test_bounce_then_find_a_new_email(db, x402_env, provider):
    _user, campaign_id, lp_id, rows = unlocked(db, provider)
    pid = rows[0]["prospect_id"]
    assert refresh(db, rows, campaign_id, "pat1@org1.example") == "Compared with the enrichers."
    verify.record_event(db, pid, "email_bounced", "pat1@org1.example", campaign_id=campaign_id)
    assert "new email: pat.new@org1.example" in refresh(db, rows, campaign_id, "pat.new@org1.example")
    outreach = db.conn.execute("SELECT contact_email FROM outreach WHERE prospect_id = ?", (pid,)).fetchone()
    assert outreach["contact_email"] == "pat.new@org1.example"
    kinds = [r["kind"] for r in db.conn.execute(
        "SELECT kind FROM contact_events WHERE prospect_id = ? ORDER BY id", (pid,)).fetchall()]
    assert kinds == ["enricher_match", "email_bounced", "enricher_mismatch"]
    # Bounce 0.5 + mismatch after a bounce 0.5, less the enricher's earlier agreement 0.25.
    check = verify.lead_verdict(db, pid)
    assert check["score"] == 0.75 and check["status"] == "verified"
    with pytest.raises(ValueError):
        verify.record_event(db, pid, "made_up")


def test_ratings_measure_each_package_and_seller(db, x402_env, provider):
    _user, campaign_id, lp_id, rows = unlocked(db, provider)
    call(db, campaign_id, rows[0]["prospect_id"], "disconnected")
    call(db, campaign_id, rows[1]["prospect_id"], "completed")
    verify.evaluate_package(db, lp_id)
    rating = claims.ratings(db)
    assert rating[(BASE, "p1")]["rate"] == 0.5 and rating[(BASE, "p1")]["unlocks"] == 1
    assert rating[BASE] == rating[(BASE, "p1")]


# ── Web ────────────────────────────────────────────────────────────────


def test_guarantee_pages_and_actions(db, x402_env, provider, monkeypatch):
    monkeypatch.setattr(lead_packages, "make_http", lambda transport=None: provider.client())
    _user, campaign_id, lp_id, rows = unlocked(db, provider)
    owner = client_for("buyer@x.com")
    pid = rows[0]["prospect_id"]
    make_user(db, "viewer@x.com", "Viewer")
    viewer = client_for("viewer@x.com")

    page = owner.get(f"/lead-packages/unlocked/{lp_id}")
    assert page.status_code == 200 and "90% phone verified" in page.text and "Not worked yet" in page.text
    assert viewer.post(f"/lead-packages/unlocked/{lp_id}/claim", data={"confirm": "yes"}).status_code == 403

    prospect_page = owner.get(f"/prospects/{pid}").text
    assert "Package guarantee" in prospect_page and "Mark email bounced" in prospect_page
    outreach_id = db.conn.execute("SELECT id FROM outreach WHERE prospect_id = ?", (pid,)).fetchone()["id"]
    owner.post(f"/prospects/{pid}/contact-event", data={"kind": "email_bounced", "outreach_id": outreach_id})
    owner.post(f"/prospects/{pid}/contact-event", data={"kind": "mail_returned", "outreach_id": outreach_id})
    prospect_page = owner.get(f"/prospects/{pid}").text
    assert "Email bounced, find a new one" in prospect_page and "Find a new email" in prospect_page
    assert "Mail returned undeliverable" in prospect_page
    bad = owner.post(f"/prospects/{pid}/contact-event", data={"kind": "made_up", "outreach_id": outreach_id})
    assert "error=" in bad.headers["location"]

    page = owner.get(f"/lead-packages/unlocked/{lp_id}").text
    assert "File claim" in page and "1 lead below 90%" in page
    unconfirmed = owner.post(f"/lead-packages/unlocked/{lp_id}/claim", data={})
    assert "error=" in unconfirmed.headers["location"] and provider.claims == []
    done = owner.post(f"/lead-packages/unlocked/{lp_id}/claim", data={"confirm": "yes"})
    assert "replacement" in done.headers["location"]
    assert "1 replacement leads" in owner.get(f"/lead-packages/unlocked/{lp_id}").text
    assert owner.post(f"/lead-packages/unlocked/{lp_id}/verify").status_code == 303
    browse = owner.get("/lead-packages").text
    assert "0% verified here" in browse and "Seller: 0% verified" in browse  # L1 failed, nothing verified yet


def test_email_send_counts_as_working_the_lead(db, x402_env, provider):
    _user, campaign_id, lp_id, rows = unlocked(db, provider)
    outreach_id = db.conn.execute("SELECT id FROM outreach WHERE prospect_id = ?",
                                  (rows[1]["prospect_id"],)).fetchone()["id"]
    db.log_email(outreach_id, campaign_id, "cold", "Hi", "", SendResult(status="sent", provider_message_id="abc"))
    assert verify.lead_verdict(db, rows[1]["prospect_id"])["status"] == "verified"
