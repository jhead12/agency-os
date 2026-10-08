"""
Tests for pulling u9itus portal events into the pipeline (spec: docs/U9ITUS_PORTAL_INTEGRATION.md).

Run: python -m pytest tests/
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.campaign import CadenceStep, CampaignConfig  # noqa: E402
from core.db import Database  # noqa: E402
from core.models import Prospect  # noqa: E402
from core.pipeline import Pipeline  # noqa: E402
from core.registry import PluginRegistry  # noqa: E402


class FakeClient:
    def is_configured(self):
        return True


class FakeU9itus:
    """Stands in for the product plugin; serves a scripted event feed and fake portals."""

    key = "fake_u9itus"
    portal_namespace = "u9itus"
    portal_label = "u9itus demo page"
    client = FakeClient()

    def is_configured(self):
        return self.client.is_configured()

    def setup_hint(self):
        return "Set FAKE_U9ITUS_TOKEN"

    def __init__(self):
        self.events = []
        self.provisioned = []

    def pull_events(self, after=0, limit=100):
        page = [e for e in self.events if e["id"] > after][:limit]
        return {"events": page, "next_cursor": page[-1]["id"] if page else after}

    def provision_demo(self, prospect, contact_email="", demo_link="", refresh=False):
        self.provisioned.append((prospect.id, contact_email, refresh))
        return {"slug": f"org-{prospect.id}", "demo_url": f"https://u9.example/portal/org-{prospect.id}?t={len(self.provisioned)}",
                "claim_url": "https://u9.example/claim", "status": "demo", "expires_at": "2026-12-01T00:00:00+00:00"}

    def get_portal_status(self, prospect):
        return {"slug": f"org-{prospect.id}", "status": "demo", "expires_at": "2026-12-01T00:00:00+00:00",
                "traffic": {"views_30d": 4, "last_viewed_on": "2026-10-02"}}

    def describe_value(self, prospect):
        return "a voter guide"

    def generate_demo_link(self, prospect, **kwargs):
        return kwargs.get("demo_link") or "https://u9.example/compare"


def event(event_id, event_type, prospect_id):
    return {"id": event_id, "type": event_type, "external_ref": f"agency-os:prospect:{prospect_id}",
            "occurred_at": "2026-10-02T12:00:00+00:00", "data": {}}


@pytest.fixture
def world(pg_url, tmp_path):
    campaign = CampaignConfig(
        name="Test Campaign", product="fake_u9itus", prospect_sources=[], channels=[],
        cadence=[CadenceStep(touch=0, delay_days=3, script="00_hello", next_stage="contacted")],
        config_dir=tmp_path / "campaign",
    )
    db = Database(pg_url)
    campaign_id = db.upsert_campaign(campaign.db_name, str(campaign.config_dir))
    product = FakeU9itus()
    registry = PluginRegistry()
    registry.products["fake_u9itus"] = product
    pipeline = Pipeline(db, registry)

    def add_prospect(stage, email=None):
        pid = db.upsert_prospect(Prospect(name=f"Org {stage}", state="CA", source="test", ein=email or stage))
        oid = db.upsert_outreach(pid, campaign_id)
        db.update_outreach(oid, {"stage": stage, "contact_email": email})
        return pid, oid


    def row(oid):
        r = db.conn.execute("SELECT stage, activity_log FROM outreach WHERE id = ?", (oid,)).fetchone()
        return r["stage"], json.loads(r["activity_log"] or "[]")

    return pipeline, campaign, product, add_prospect, row


def test_view_then_claim_move_the_prospect_forward(world):
    pipeline, campaign, product, add_prospect, row = world
    pid, oid = add_prospect("contacted")
    product.events = [event(1, "portal.viewed", pid), event(2, "portal.claimed", pid)]

    stats = pipeline.pull_product_events(campaign)

    assert row(oid)[0] == "demo_scheduled"
    assert stats["stage_changes"] == 2


def test_a_lone_publish_event_flags_ready_to_close_without_changing_stage(world):
    # Regression: a publish arriving in a later pull than the claim used to crash
    # with UnboundLocalError (json imported inside a branch that was skipped).
    pipeline, campaign, product, add_prospect, row = world
    pid, oid = add_prospect("demo_scheduled")
    product.events = [event(7, "portal.published", pid)]

    pipeline.pull_product_events(campaign)

    stage, log = row(oid)
    assert stage == "demo_scheduled"
    assert [(a["type"], a.get("flag")) for a in log] == [("portal_published", "ready_to_close")]


def test_publish_never_advances_an_engaged_prospect(world):
    pipeline, campaign, product, add_prospect, row = world
    pid, oid = add_prospect("engaged")
    product.events = [event(1, "portal.published", pid)]

    pipeline.pull_product_events(campaign)

    assert row(oid)[0] == "engaged"


def test_events_never_move_a_prospect_backward_or_out_of_a_closed_stage(world):
    pipeline, campaign, product, add_prospect, row = world
    won_pid, won_oid = add_prospect("closed_won")
    sent_pid, sent_oid = add_prospect("proposal_sent")
    product.events = [event(1, "portal.viewed", won_pid), event(2, "portal.claimed", sent_pid)]

    pipeline.pull_product_events(campaign)

    assert row(won_oid)[0] == "closed_won"
    assert row(sent_oid)[0] == "proposal_sent"


def test_pulling_twice_changes_nothing_the_second_time(world):
    pipeline, campaign, product, add_prospect, row = world
    pid, oid = add_prospect("contacted")
    product.events = [event(1, "portal.viewed", pid)]

    pipeline.pull_product_events(campaign)
    second = pipeline.pull_product_events(campaign)

    assert second["stage_changes"] == 0 and second["events_pulled"] == 0
    assert len(row(oid)[1]) == 1


def test_events_for_unknown_prospects_are_ignored(world):
    pipeline, campaign, product, add_prospect, row = world
    product.events = [event(1, "portal.viewed", 9999), {**event(2, "portal.viewed", 1), "external_ref": "other-system:1"}]

    stats = pipeline.pull_product_events(campaign)

    assert stats["stage_changes"] == 0


def test_a_claim_is_logged_even_when_the_stage_does_not_change(world):
    # A10 relies on the portal.claimed entry when a Calendly booking already set demo_scheduled.
    pipeline, campaign, product, add_prospect, row = world
    pid, oid = add_prospect("demo_scheduled")
    product.events = [event(1, "portal.claimed", pid)]

    pipeline.pull_product_events(campaign)

    stage, log = row(oid)
    assert stage == "demo_scheduled"
    assert [a["type"] for a in log] == ["portal.claimed"]


def test_a_view_revives_a_nurture_prospect(world):
    pipeline, campaign, product, add_prospect, row = world
    pid, oid = add_prospect("nurture")
    product.events = [event(1, "portal.viewed", pid)]

    pipeline.pull_product_events(campaign)

    assert row(oid)[0] == "engaged"


def test_an_event_reaches_the_prospect_in_every_campaign(world, tmp_path):
    # The cursor is per product; a second campaign's rows must not be skipped.
    pipeline, campaign, product, add_prospect, row = world
    pid, oid = add_prospect("contacted")
    db = pipeline.db
    other_campaign_id = db.upsert_campaign("other-campaign", str(tmp_path / "other"))
    other_oid = db.upsert_outreach(pid, other_campaign_id)
    db.update_outreach(other_oid, {"stage": "contacted"})
    product.events = [event(1, "portal.viewed", pid)]

    pipeline.pull_product_events(campaign)

    assert row(oid)[0] == "engaged" and row(other_oid)[0] == "engaged"


def test_an_expired_demo_clears_the_link_so_it_can_be_renewed(world):
    pipeline, campaign, product, add_prospect, row = world
    pid, oid = add_prospect("contacted")
    pipeline.db.update_outreach(oid, {"demo_link": "https://u9.example/portal/old"})
    product.events = [event(1, "portal.expired", pid)]

    pipeline.pull_product_events(campaign)

    assert pipeline.db.get_outreach(oid).demo_link is None
    assert pipeline.db.get_prospect(pid).metadata["u9itus"]["status"] == "expired"


def test_provisioning_one_prospect_stores_the_link_and_status(world):
    pipeline, campaign, product, add_prospect, row = world
    pid, oid = add_prospect("engaged", email="ed@org.example")

    result = pipeline.provision_prospect(campaign, pid)

    assert result["status"] == "demo"
    assert product.provisioned == [(pid, "ed@org.example", False)]
    assert pipeline.db.get_outreach(oid).demo_link.startswith("https://u9.example/portal/")
    assert pipeline.db.get_prospect(pid).metadata["u9itus"]["slug"] == f"org-{pid}"

    pipeline.refresh_portal_status(campaign, pid)
    meta = pipeline.db.get_prospect(pid).metadata["u9itus"]
    assert meta["views_30d"] == 4 and meta["slug"] == f"org-{pid}"


def test_emails_use_the_provisioned_demo_link(world):
    pipeline, campaign, product, add_prospect, row = world
    pid, oid = add_prospect("contacted", email="ed@org.example")
    pipeline.db.update_outreach(oid, {"demo_link": "https://u9.example/portal/org-x?t=abc"})

    variables = pipeline._build_variables(campaign, pipeline.db.get_prospect(pid), pipeline.db.get_outreach(oid))

    assert variables["demo_link"] == "https://u9.example/portal/org-x?t=abc"


# ── Product-defined event effects (core/protocols.py event_effect) ──────

from core.protocols import EventEffect  # noqa: E402
from plugins.products.u9itus_voter_guide import U9itusVoterGuideProduct  # noqa: E402


def paid(event_id, prospect_id, plan="pro"):
    return {**event(event_id, "subscription.activated", prospect_id),
            "data": {"plan": plan, "amount_cents": 150000, "cycle": "2026-general"}}


def test_a_paid_plan_flags_the_deal_and_records_plan_and_amount(world):
    pipeline, campaign, product, add_prospect, row = world
    product.event_effect = U9itusVoterGuideProduct().event_effect
    pid, oid = add_prospect("demo_scheduled")
    product.events = [paid(1, pid)]

    pipeline.pull_product_events(campaign)

    stage, log = row(oid)
    assert stage == "demo_scheduled"  # the rep closes the deal, not the event
    assert [{k: a.get(k) for k in ("type", "flag", "plan", "amount_cents", "cycle")} for a in log] == [
        {"type": "subscription.activated", "flag": "ready_to_close", "plan": "pro",
         "amount_cents": 150000, "cycle": "2026-general"}]
    meta = pipeline.db.get_prospect(pid).metadata["u9itus"]
    assert (meta["subscription_status"], meta["plan"], meta["cycle"]) == ("active", "pro", "2026-general")


def test_expiry_and_cancel_are_noted_but_never_move_a_closed_deal(world):
    pipeline, campaign, product, add_prospect, row = world
    product.event_effect = U9itusVoterGuideProduct().event_effect
    pid, oid = add_prospect("closed_won")
    product.events = [
        {**event(1, "subscription.expired", pid), "data": {"cycle": "2026-general"}},
        {**event(2, "subscription.canceled", pid), "data": {}},
    ]

    stats = pipeline.pull_product_events(campaign)

    stage, log = row(oid)
    assert stage == "closed_won" and stats["stage_changes"] == 0
    assert [(a["type"], a.get("cycle")) for a in log] == [
        ("subscription.expired", "2026-general"), ("subscription.canceled", None)]
    assert pipeline.db.get_prospect(pid).metadata["u9itus"]["subscription_status"] == "canceled"


def test_the_u9itus_plugin_keeps_the_shared_portal_events():
    plugin = U9itusVoterGuideProduct()
    assert plugin.event_effect({"type": "portal.claimed"}).stage == "demo_scheduled"
    assert plugin.event_effect({"type": "portal.expired"}).clear_demo_link
    assert plugin.event_effect({"type": "something.new"}) is None


def test_an_unknown_event_is_recorded_and_changes_nothing(world):
    pipeline, campaign, product, add_prospect, row = world
    pid, oid = add_prospect("contacted")
    product.events = [event(1, "subscription.activated", pid)]  # FakeU9itus has no event_effect

    stats = pipeline.pull_product_events(campaign)

    assert row(oid) == ("contacted", [])
    assert stats["events_pulled"] == 1
    recorded = pipeline.db.conn.execute("SELECT event_type FROM product_events").fetchall()
    assert [r["event_type"] for r in recorded] == ["subscription.activated"]


def test_any_product_can_bring_its_own_events_without_core_changes(world):
    pipeline, campaign, product, add_prospect, row = world
    product.event_effect = lambda e: (EventEffect(stage="engaged", log_as="guide.shared", detail={"via": "sms"})
                                      if e["type"] == "guide.shared" else None)
    pid, oid = add_prospect("contacted")
    product.events = [event(1, "guide.shared", pid)]

    assert pipeline.pull_product_events(campaign)["stage_changes"] == 1
    stage, log = row(oid)
    assert stage == "engaged" and log[0]["via"] == "sms" and log[0]["stage"] == "engaged"


# ── Prices come from u9itus ──────────────────────────────────────────


class PlansClient:
    def __init__(self, result):
        self.result, self.calls = result, 0

    def is_configured(self):
        return True

    def get_plans(self):
        self.calls += 1
        return self.result


def test_pricing_tiers_use_u9itus_prices_and_cache_them():
    plugin = U9itusVoterGuideProduct()
    plugin._client = PlansClient({"plans": [
        {"key": "starter", "label": "Starter", "amount_cents": 60000},
        {"key": "pro", "label": "Pro", "amount_cents": 175050},
    ]})

    tiers = plugin.pricing_tiers()
    plugin.pricing_tiers()

    assert [(t["key"], t["name"], t["price"]) for t in tiers] == [("starter", "Starter", 600), ("pro", "Pro", 1750.5)]
    assert tiers[0]["features"][0] == "Co-branded voter guide with your logo"
    assert plugin._client.calls == 1


def test_pricing_tiers_fall_back_when_u9itus_is_unreachable():
    plugin = U9itusVoterGuideProduct()
    plugin._client = PlansClient({"error": True, "status": 404, "detail": "not found"})

    assert [t["price"] for t in plugin.pricing_tiers()] == [500, 1500, 4000]
    plugin.pricing_tiers()
    assert plugin._client.calls == 2  # a failure isn't cached; the next call tries again
