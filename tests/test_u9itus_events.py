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
    client = FakeClient()

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
