"""
Tests that demo-page support isn't tied to u9itus: any product implementing
DemoPortalProduct (core/protocols.py) gets provisioning, event pulls and stage
moves, with its details kept under its own portal_namespace.

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
from core.protocols import portal_product  # noqa: E402
from core.registry import PluginRegistry  # noqa: E402
from tests.test_u9itus_events import FakeU9itus, event  # noqa: E402


class FakeTrialProduct:
    """A second portal product whose API names its events differently."""

    key = "fake_trial"
    portal_namespace = "trial"
    portal_label = "Trial workspace"

    # This API's event names → the shared portal.* vocabulary.
    EVENT_TYPES = {"workspace.opened": "portal.viewed", "workspace.activated": "portal.claimed"}

    def __init__(self):
        self.raw_events = []
        self.configured = True

    def is_configured(self):
        return self.configured

    def setup_hint(self):
        return "Set TRIAL_API_KEY"

    def provision_demo(self, prospect, contact_email="", refresh=False):
        return {"slug": f"ws-{prospect.id}", "demo_url": f"https://trial.example/ws-{prospect.id}",
                "claim_url": "https://trial.example/activate", "status": "demo"}

    def get_portal_status(self, prospect):
        return {"status": "demo", "traffic": {"views_30d": 9}}

    def pull_events(self, after=0, limit=100):
        page = [e for e in self.raw_events if e["id"] > after][:limit]
        events = [{**e, "type": self.EVENT_TYPES.get(e["type"], e["type"])} for e in page]
        return {"events": events, "next_cursor": page[-1]["id"] if page else after}

    def describe_value(self, prospect):
        return "a trial workspace"

    def generate_demo_link(self, prospect, **kwargs):
        return kwargs.get("demo_link")


class PlainProduct:
    """A product with no demo pages at all."""

    key = "plain"

    def describe_value(self, prospect):
        return "a thing"

    def generate_demo_link(self, prospect, **kwargs):
        return None

    def pricing_tiers(self):
        return []


@pytest.fixture
def world(pg_url, tmp_path):
    db = Database(pg_url)
    registry = PluginRegistry()
    products = {"fake_u9itus": FakeU9itus(), "fake_trial": FakeTrialProduct(), "plain": PlainProduct()}
    registry.products.update(products)
    pipeline = Pipeline(db, registry)

    campaigns = {}
    for key in products:
        campaign = CampaignConfig(
            name=f"Campaign {key}", product=key, prospect_sources=[], channels=[],
            cadence=[CadenceStep(touch=0, delay_days=3, script="00_hello", next_stage="contacted")],
            config_dir=tmp_path / key,
        )
        campaigns[key] = (campaign, db.upsert_campaign(campaign.db_name, str(campaign.config_dir)))

    def enroll(pid, product_key, stage="contacted", email="ed@org.example"):
        oid = db.upsert_outreach(pid, campaigns[product_key][1])
        db.update_outreach(oid, {"stage": stage, "contact_email": email})
        return oid

    def metadata(pid):
        return json.loads(db.conn.execute("SELECT metadata FROM prospects WHERE id = ?", (pid,)).fetchone()["metadata"] or "{}")

    def log(oid):
        row = db.conn.execute("SELECT activity_log FROM outreach WHERE id = ?", (oid,)).fetchone()
        return json.loads(row["activity_log"] or "[]")

    pid = db.upsert_prospect(Prospect(name="Eastside Families United", state="CA", source="test", ein="11-1111111"))
    return pipeline, db, products, {k: c for k, (c, _) in campaigns.items()}, pid, enroll, metadata, log


def test_only_products_with_demo_pages_count_as_portal_products(world):
    _, _, products, *_ = world
    assert portal_product(products["fake_trial"]) is products["fake_trial"]
    assert portal_product(products["fake_u9itus"]) is products["fake_u9itus"]
    assert portal_product(products["plain"]) is None
    assert portal_product(None) is None


def test_a_second_product_provisions_into_its_own_namespace(world):
    pipeline, db, _, campaigns, pid, enroll, metadata, _ = world
    oid = enroll(pid, "fake_trial")

    result = pipeline.provision_prospect(campaigns["fake_trial"], pid)

    assert result["slug"] == f"ws-{pid}"
    assert metadata(pid) == {"trial": {"slug": f"ws-{pid}", "claim_url": "https://trial.example/activate",
                                       "status": "demo", "expires_at": None}}
    assert db.get_outreach(oid).demo_link == f"https://trial.example/ws-{pid}"


def test_translated_events_move_the_stage_and_log_under_the_namespace(world):
    pipeline, db, products, campaigns, pid, enroll, metadata, log = world
    oid = enroll(pid, "fake_trial")
    products["fake_trial"].raw_events = [
        {**event(1, "workspace.opened", pid)}, {**event(2, "workspace.activated", pid)},
    ]

    stats = pipeline.pull_product_events(campaigns["fake_trial"])

    assert stats["events_pulled"] == 2
    assert db.get_outreach(oid).stage == "demo_scheduled"
    assert [(e["type"], e["ref"]) for e in log(oid)] == [("portal.viewed", "trial:1"), ("portal.claimed", "trial:2")]
    assert metadata(pid)["trial"]["status"] == "claimed"
    assert "u9itus" not in metadata(pid)


def test_two_portal_products_on_one_prospect_keep_separate_records(world):
    pipeline, _, products, campaigns, pid, enroll, metadata, log = world
    u9_oid, trial_oid = enroll(pid, "fake_u9itus"), enroll(pid, "fake_trial")
    pipeline.provision_prospect(campaigns["fake_u9itus"], pid)
    pipeline.provision_prospect(campaigns["fake_trial"], pid)
    # Both feeds use event id 1; neither may be mistaken for the other.
    products["fake_u9itus"].events = [event(1, "portal.viewed", pid)]
    products["fake_trial"].raw_events = [event(1, "workspace.opened", pid)]

    pipeline.pull_product_events(campaigns["fake_u9itus"])
    pipeline.pull_product_events(campaigns["fake_trial"])

    assert metadata(pid)["u9itus"]["slug"] == f"org-{pid}"
    assert metadata(pid)["trial"]["slug"] == f"ws-{pid}"
    # A portal event applies to all of the prospect's outreach rows, so each
    # row carries one entry per product.
    for oid in (u9_oid, trial_oid):
        assert sorted(e["ref"] for e in log(oid)) == ["trial:1", "u9itus:1"]


def test_an_unconfigured_product_reports_its_own_setup_hint(world):
    pipeline, _, products, campaigns, pid, enroll, *_ = world
    enroll(pid, "fake_trial")
    products["fake_trial"].configured = False

    assert pipeline.pull_product_events(campaigns["fake_trial"])["setup_hint"] == "Set TRIAL_API_KEY"
    assert pipeline.provision_demos(campaigns["fake_trial"])["setup_hint"] == "Set TRIAL_API_KEY"
    assert "TRIAL_API_KEY" in pipeline.provision_prospect(campaigns["fake_trial"], pid)["detail"]


def test_a_product_without_demo_pages_is_left_alone(world):
    pipeline, _, _, campaigns, pid, enroll, metadata, _ = world
    enroll(pid, "plain")

    assert "error" in pipeline.provision_demos(campaigns["plain"])
    assert "error" in pipeline.pull_product_events(campaigns["plain"])
    assert pipeline.provision_prospect(campaigns["plain"], pid)["error"] is True
    assert metadata(pid) == {}


def test_the_real_u9itus_product_keeps_its_existing_namespace():
    from plugins.products.u9itus_voter_guide import U9itusVoterGuideProduct

    product = portal_product(U9itusVoterGuideProduct())
    # Existing prospect metadata and activity-log refs are stored under "u9itus".
    assert product is not None and product.portal_namespace == "u9itus"
    assert hasattr(product, "check_connection") and hasattr(product, "create_test_portal")
