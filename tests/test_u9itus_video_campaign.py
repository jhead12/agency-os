"""
Tests for the u9itus video campaign product, church detection, YouTube
discovery and the shared u9itus event feed (u9itus doc/AGENCY_OS_INTEGRATION.md
section 13).

Run: python -m pytest tests/
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.campaign import CadenceStep, CampaignConfig  # noqa: E402
from core.db import Database  # noqa: E402
from core.models import EnrichmentResult, Prospect  # noqa: E402
from core.pipeline import Pipeline  # noqa: E402
from core.protocols import event_feed, shared_event_effect  # noqa: E402
from core.registry import PluginRegistry  # noqa: E402
from plugins.enrichers.local_scraper import LocalScraperEnricher  # noqa: E402
from plugins.products.u9itus_video_campaign import U9itusVideoCampaignProduct  # noqa: E402
from plugins.products.u9itus_voter_guide import U9itusVoterGuideProduct  # noqa: E402
from tests.test_u9itus_events import FakeU9itus, event  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


class FakeClient:
    """Records provision calls and answers like the u9itus agency API."""

    def __init__(self):
        self.calls = []

    def is_configured(self):
        return True

    def provision_demo(self, **kwargs):
        self.calls.append(kwargs)
        return {"slug": "grace", "demo_url": "https://u9.example/portal/grace?t=x",
                "video_demo_url": "https://u9.example/portal/grace/video-demo?t=x",
                "claim_url": "https://u9.example/claim", "status": "demo"}

    def get_plans(self):
        return {"plans": [], "video": {"revenue_per_view_cents": 80, "voter_payout_per_view_cents": 50}}


def video_product():
    product = U9itusVideoCampaignProduct()
    product._client = FakeClient()
    product._rate_cache = None
    return product


def church(**metadata):
    return Prospect(id=7, name="Grace Community Church", state="CA", metadata=metadata)


# ── Product ───────────────────────────────────────────────────────────

def test_the_video_demo_link_becomes_the_email_demo_link():
    product = video_product()

    result = product.provision_demo(church(youtube_url="https://www.youtube.com/watch?v=abcdefghijk"), contact_email="pastor@grace.example")

    assert result["demo_url"] == "https://u9.example/portal/grace/video-demo?t=x"
    assert result["portal_demo_url"] == "https://u9.example/portal/grace?t=x"
    call = product.client.calls[0]
    assert call["video_url"] == "https://www.youtube.com/watch?v=abcdefghijk" and call["youtube_channel"] == ""
    assert call["external_ref"] == "agency-os:prospect:7"


def test_a_channel_link_is_sent_as_youtube_channel():
    product = video_product()

    product.provision_demo(church(youtube_url="https://www.youtube.com/@gracechurch"))

    assert product.client.calls[0]["youtube_channel"] == "https://www.youtube.com/@gracechurch"
    assert product.client.calls[0]["video_url"] == ""


def test_before_provisioning_the_email_links_to_the_about_page_not_a_placeholder():
    product = video_product()

    assert product.generate_demo_link(church()) == "https://www.u9itus.com/about"
    assert product.generate_demo_link(church(), demo_link="https://u9.example/v") == "https://u9.example/v"


def test_view_packs_are_priced_at_the_u9itus_per_view_rate():
    tiers = video_product().pricing_tiers()

    assert [(t["key"], t["price"]) for t in tiers] == [("reach_500", 400), ("reach_2000", 1600), ("reach_5000", 4000)]


def test_the_pitch_names_the_congregation_for_churches():
    product = video_product()

    assert "congregation" in product.describe_value(church(irs_foundation="10"))
    assert "people in your community" in product.describe_value(Prospect(name="Eastside", metadata={}))


# ── Churches ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("metadata, expected", [
    ({"irs_foundation": "10", "irs_subsection": "03"}, "church"),
    ({"ntee_full": "X21", "irs_subsection": "03"}, "church"),
    ({"ntee_full": "X80", "irs_subsection": "03"}, "c3_nonprofit"),  # religious media, not a congregation
    ({"ntee_full": "P20", "irs_subsection": "03"}, "c3_nonprofit"),
])
def test_churches_provision_as_the_church_org_type(metadata, expected):
    assert U9itusVoterGuideProduct()._to_org_type(church(**metadata)) == expected


def test_the_church_campaign_finds_churches_without_an_ntee_code():
    campaign = CampaignConfig.load(ROOT / "campaigns" / "video-campaign-churches")

    assert campaign.product == "u9itus_video_campaign"
    assert campaign.filters["foundation_codes"] == ["10"]


def test_the_video_campaigns_load_with_their_scripts():
    for name in ("video-campaign-cbo", "video-campaign-churches"):
        directory = ROOT / "campaigns" / name
        campaign = CampaignConfig.load(directory)
        for step in campaign.cadence:
            assert (directory / "scripts" / f"{step.script}.yaml").exists(), (name, step.script)


# ── YouTube discovery ─────────────────────────────────────────────────

def test_the_scraper_prefers_a_channel_link_over_a_video():
    pages = [("https://grace.example", '<iframe src="https://www.youtube-nocookie.com/embed/abcdefghijk"></iframe>'),
             ("https://grace.example/about", '<a href="https://www.youtube.com/@gracechurch">YouTube</a>')]

    assert LocalScraperEnricher()._extract_youtube(pages) == "https://www.youtube.com/@gracechurch"


def test_the_scraper_falls_back_to_an_embedded_video():
    pages = [("https://grace.example", '<iframe src="https://www.youtube-nocookie.com/embed/abcdefghijk"></iframe>')]

    assert LocalScraperEnricher()._extract_youtube(pages) == "https://www.youtube.com/embed/abcdefghijk"


def test_a_found_youtube_link_is_saved_on_the_prospect(pg_url):
    db = Database(pg_url)
    pid = db.upsert_prospect(Prospect(name="Grace Community Church", state="CA", source="test", ein="22-2222222"))
    oid = db.upsert_outreach(pid, db.upsert_campaign("video_test", "/tmp/video_test"))

    db.apply_enrichment(oid, EnrichmentResult(source="local_scraper", raw={"youtube_url": "https://www.youtube.com/@gracechurch"}), prospect_id=pid)

    row = db.conn.execute("SELECT metadata FROM prospects WHERE id = ?", (pid,)).fetchone()
    assert json.loads(row["metadata"])["youtube_url"] == "https://www.youtube.com/@gracechurch"


# ── Events ───────────────────────────────────────────────────────────

def test_opening_the_video_demo_counts_as_engagement():
    effect = shared_event_effect({"type": "portal.video_demo_viewed"})

    assert effect.stage == "engaged" and effect.log_as == "portal.video_demo_viewed"


def test_both_u9itus_products_read_one_feed_under_the_existing_cursor():
    assert event_feed(U9itusVideoCampaignProduct(), "u9itus_video_campaign") == "u9itus_voter_guide"
    assert event_feed(U9itusVoterGuideProduct(), "u9itus_voter_guide") == "u9itus_voter_guide"
    assert event_feed(FakeU9itus(), "fake_u9itus") == "fake_u9itus"


def test_products_sharing_a_feed_apply_each_event_once(pg_url, tmp_path):
    db = Database(pg_url)
    registry = PluginRegistry()
    feed = FakeU9itus()
    feed.event_feed = "shared_feed"
    twin = FakeU9itus()
    twin.event_feed = "shared_feed"
    twin.events = feed.events
    registry.products.update({"guide": feed, "video": twin})
    pipeline = Pipeline(db, registry)

    campaigns = {}
    for key in ("guide", "video"):
        campaign = CampaignConfig(name=f"Campaign {key}", product=key, prospect_sources=[], channels=[],
                                  cadence=[CadenceStep(touch=0, delay_days=3, script="00_hello", next_stage="contacted")],
                                  config_dir=tmp_path / key)
        campaigns[key] = (campaign, db.upsert_campaign(campaign.db_name, str(campaign.config_dir)))

    pid = db.upsert_prospect(Prospect(name="Grace Community Church", state="CA", source="test", ein="33-3333333"))
    oid = db.upsert_outreach(pid, campaigns["video"][1])
    db.update_outreach(oid, {"stage": "contacted", "contact_email": "pastor@grace.example"})
    feed.events.append(event(1, "portal.video_demo_viewed", pid))

    pipeline.pull_product_events(campaigns["guide"][0])
    second = pipeline.pull_product_events(campaigns["video"][0])

    row = db.conn.execute("SELECT stage, activity_log FROM outreach WHERE id = ?", (oid,)).fetchone()
    assert row["stage"] == "engaged"
    assert [e["type"] for e in json.loads(row["activity_log"])] == ["portal.video_demo_viewed"]
    assert second["events_pulled"] == 0
