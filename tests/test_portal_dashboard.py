"""
Tests for the u9itus demo-page card on the prospect page (A6) and the
background job runner and Jobs page (A7).

Run: python -m pytest tests/
"""

import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import web.app as webapp  # noqa: E402
from core import jobs  # noqa: E402
from core.models import Prospect  # noqa: E402
from core.registry import PluginRegistry  # noqa: E402
from tests.test_access import client_for, db, make_user  # noqa: E402,F401
from tests.test_u9itus_events import FakeU9itus  # noqa: E402

PRODUCT_KEY = "u9itus_voter_guide"


@pytest.fixture
def fake_product(monkeypatch):
    """Swap the real u9itus product (which calls the API) for a fake, everywhere."""
    product = FakeU9itus()
    product.key = PRODUCT_KEY

    def registry():
        r = PluginRegistry()
        r.products[PRODUCT_KEY] = product
        return r

    monkeypatch.setattr(webapp, "_plugin_registry", registry)
    monkeypatch.setattr(jobs, "PluginRegistry", lambda: _Discoverless(registry()))
    return product


class _Discoverless:
    """A registry whose discover() is a no-op, for JobRunner."""

    def __init__(self, registry):
        self._registry = registry

    def discover(self, *_args, **_kwargs):
        pass

    def __getattr__(self, name):
        return getattr(self._registry, name)


def add_prospect(db, stage="contacted", email="ed@org.example"):
    campaign = next(c for c in webapp.get_campaigns() if c.product == PRODUCT_KEY)
    campaign_id = db.upsert_campaign(campaign.db_name, str(campaign.config_dir / "campaign.yaml"))
    pid = db.upsert_prospect(Prospect(name="Eastside Families United", state="CA", source="test", ein="11-1111111"))
    oid = db.upsert_outreach(pid, campaign_id)
    db.update_outreach(oid, {"stage": stage, "contact_email": email})
    return pid, oid


# ── Prospect page (A6) ─────────────────────────────────────────────────


def test_a_sales_rep_can_create_a_demo_page_and_see_it(db, fake_product):
    make_user(db, "rep@agency.example", "Sales Rep")
    pid, oid = add_prospect(db)
    client = client_for("rep@agency.example")

    page = client.get(f"/prospects/{pid}").text
    assert "No demo page yet" in page and "Create demo page" in page

    r = client.post(f"/prospects/{pid}/portal", data={"action": "provision"})
    assert r.status_code == 303 and "Demo%20page%20ready" in r.headers["location"]

    page = client.get(f"/prospects/{pid}").text
    assert "Demo live" in page and "Open demo" in page and "Create demo page" not in page
    assert db.get_outreach(oid).demo_link.startswith("https://u9.example/portal/")


def test_refresh_status_shows_views(db, fake_product):
    make_user(db, "rep@agency.example", "Sales Rep")
    pid, _ = add_prospect(db)
    client = client_for("rep@agency.example")
    client.post(f"/prospects/{pid}/portal", data={"action": "provision"})

    client.post(f"/prospects/{pid}/portal", data={"action": "status"})

    assert "Views (30 days)" in client.get(f"/prospects/{pid}").text


def test_portal_events_and_ready_to_close_badge_show_on_the_prospect(db, fake_product):
    make_user(db, "rep@agency.example", "Sales Rep")
    pid, oid = add_prospect(db, stage="demo_scheduled")
    db.update_outreach(oid, {"activity_log": json.dumps([
        {"type": "portal.claimed", "ref": "u9itus:2", "timestamp": "2026-10-02T10:00:00+00:00"},
        {"type": "portal_published", "ref": "u9itus:3", "flag": "ready_to_close", "timestamp": "2026-10-02T11:00:00+00:00"},
    ])})

    page = client_for("rep@agency.example").get(f"/prospects/{pid}").text

    assert "ready to close" in page and "Claimed the page" in page and "Published the page" in page


def test_a_caller_cannot_create_demo_pages(db, fake_product):
    make_user(db, "caller@agency.example", "Caller")
    pid, _ = add_prospect(db)
    client = client_for("caller@agency.example")

    assert "Create demo page" not in client.get(f"/prospects/{pid}").text
    assert client.post(f"/prospects/{pid}/portal", data={"action": "provision"}).status_code == 403
    assert fake_product.provisioned == []


def test_an_api_error_is_shown_not_raised(db, fake_product):
    make_user(db, "rep@agency.example", "Sales Rep")
    pid, _ = add_prospect(db)
    fake_product.provision_demo = lambda *a, **k: {"error": True, "detail": "unauthorized"}

    r = client_for("rep@agency.example").post(f"/prospects/{pid}/portal", data={"action": "provision"})

    assert r.status_code == 303 and "error=u9itus" in r.headers["location"]


# ── Jobs (A7) ──────────────────────────────────────────────────────────


def runner():
    return webapp.get_job_runner()


def test_jobs_are_off_unless_enabled(monkeypatch):
    monkeypatch.delenv("AGENCY_OS_RUN_JOBS", raising=False)
    assert jobs.jobs_enabled() is False
    monkeypatch.setenv("AGENCY_OS_RUN_JOBS", "1")
    assert jobs.jobs_enabled() is True


def test_a_job_run_is_recorded_and_not_due_again_until_its_interval(db, fake_product):
    pid, oid = add_prospect(db)

    assert {j.key for j in runner().due_jobs()} == {"pull-events", "provision"}
    result = runner().run("provision", "manual")

    assert result["ok"] is True
    assert db.get_outreach(oid).demo_link is not None
    assert "provision" not in {j.key for j in runner().due_jobs()}
    later = datetime.now() + timedelta(days=1, minutes=1)
    assert "provision" in {j.key for j in runner().due_jobs(now=later)}


def test_pull_events_runs_once_per_product(db, fake_product):
    pid, oid = add_prospect(db)
    fake_product.events = [{"id": 1, "type": "portal.viewed", "external_ref": f"agency-os:prospect:{pid}",
                            "occurred_at": "2026-10-02T12:00:00+00:00", "data": {}}]

    result = runner().run("pull-events")

    assert result["ok"] is True and list(result["summary"]) == [PRODUCT_KEY]
    assert db.get_outreach(oid).stage == "engaged"


def test_an_unconfigured_api_is_reported_as_a_problem(db, fake_product):
    add_prospect(db)
    fake_product.client = type("Off", (), {"is_configured": lambda self: False})()

    assert runner().run("pull-events")["ok"] is False


def test_jobs_page_is_owner_only_and_run_now_records_a_run(db, fake_product):
    make_user(db, "owner@agency.example", "Owner")
    make_user(db, "rep@agency.example", "Sales Rep")
    add_prospect(db)

    assert client_for("rep@agency.example").get("/admin/jobs").status_code == 403

    owner = client_for("owner@agency.example")
    page = owner.get("/admin/jobs").text
    assert "Scheduled runs are off" in page and "Run now" in page

    r = owner.post("/admin/jobs/provision/run")
    assert r.status_code == 303 and "msg=Ran" in r.headers["location"]
    assert db.conn.execute("SELECT trigger, ok FROM job_runs").fetchall()[0][:] == ("manual", 1)
