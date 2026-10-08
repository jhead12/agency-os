"""
The lead package generator (core/generator.py): criteria → searches → house
prospects in a campaign → enrichers → a saved list that publishes at the
`enriched` guarantee. Search plugins and enrichers are faked; nothing touches the network.

Run: TEST_DATABASE_URL=postgresql://localhost/agency_os_test python -m pytest tests/test_generator.py
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import web.app as webapp  # noqa: E402
from core import access, accounts, generator, selling  # noqa: E402
from core.models import EnrichmentResult, Prospect  # noqa: E402
from tests.test_access import client_for, db, make_user  # noqa: E402,F401
from tests.test_search_runner import FakeSearch, shop  # noqa: E402

SUPER = "root@x.com"


class FakeEnricher:
    """Finds an email for even-numbered shops only."""

    def __init__(self):
        self.calls = 0

    def is_configured(self):
        return True

    def enrich(self, prospect):
        self.calls += 1
        n = int(prospect.name.split()[-1])
        return EnrichmentResult(contact_email=f"hello@shop{n}.test") if n % 2 == 0 else EnrichmentResult()


class FakeRegistry:
    def __init__(self, enricher):
        self.enrichers = {"fake": enricher}

    def get_enricher(self, key):
        return self.enrichers.get(key)


class Failing:
    def validate(self, params):
        return params

    def run(self, params, ctx):
        raise ValueError("The map data service is busy.")
        yield  # noqa: unreachable — makes this a generator


@pytest.fixture
def setup(db):
    user_id = make_user(db, SUPER, access.SUPER_ADMIN_ROLE)
    campaign_id = db.upsert_campaign("gen", "x")
    return db.load_current_user(user_id), campaign_id


def start(db, user, searches_wanted, max_leads=4, available=None, enrichers=("fake",)):
    available = available or {"city": FakeSearch(count=10)}
    return generator.create(db, user, title="Austin shops", campaign_name="gen", searches_wanted=searches_wanted,
                            enrichers=list(enrichers), max_leads=max_leads, available=available)


def finish(db, available, enricher=None):
    runner = generator.Runner(db.url, available=available, registry=FakeRegistry(enricher or FakeEnricher()))
    return runner.run_pending()


CITY = [{"type": "city", "params": {}}]


def test_a_run_finds_enriches_and_makes_a_sellable_saved_list(db, setup):
    user, campaign_id = setup
    plugin = FakeSearch(count=10)
    start(db, user, CITY, max_leads=4, available={"city": plugin})
    enricher = FakeEnricher()
    [run] = finish(db, {"city": plugin}, enricher)
    assert (run["status"], run["found"], run["enriched"], run["eligible"], run["error"]) == ("done", 4, 2, 2, None)
    assert enricher.calls == 4

    leads = generator.lead_ids(db, run["id"])
    assert len(leads) == 4
    rows = db.conn.execute("SELECT prospect_id, campaign_id, stage FROM outreach WHERE prospect_id = ANY(?)",
                           (leads,)).fetchall()
    assert {(r["campaign_id"], r["stage"]) for r in rows} == {(campaign_id, "cold")}
    assert db.conn.execute("SELECT COUNT(*) FROM prospects WHERE id = ANY(?) AND account_id IS NULL",
                           (leads,)).fetchone()[0] == 4  # house prospects

    saved = db.get_prospect_saved_list(run["saved_list_id"])
    assert saved["criteria"] == {"generator_run": str(run["id"])} and "generated #" in saved["name"]
    where, params = db.prospect_filter(saved["criteria"])
    matched = {r["id"] for r in db.conn.execute(
        f"SELECT DISTINCT p.id FROM prospects p LEFT JOIN outreach o ON p.id = o.prospect_id WHERE {where}",
        params).fetchall()}
    assert matched == set(leads)

    package_id, problem = selling.publish(
        db, user, saved_list_id=run["saved_list_id"], title="Austin shops", industry="retail", region="Austin, TX",
        unlock_usd="5", royalty_usd={"enriched": "0.02"}, guarantee_tier="enriched", rules=None,
        consent_note="OpenStreetMap; emails from shop websites", sms_consent=False)
    assert problem == ""
    tiers = [r["tier"] for r in db.conn.execute(
        "SELECT tier FROM published_leads WHERE package_id = ?", (package_id,)).fetchall()]
    assert tiers == ["enriched", "enriched"]  # only the two with an email


def test_a_found_business_phone_counts_as_a_contact(db, setup):
    user, _ = setup

    class WithPhone(FakeSearch):
        def run(self, params, ctx):
            for p in super().run(params, ctx):
                p.metadata = {"phone": "512-555-0100"}
                yield p
    plugin = WithPhone(count=3)
    start(db, user, CITY, max_leads=3, available={"city": plugin}, enrichers=())
    [run] = finish(db, {"city": plugin})
    assert (run["found"], run["enriched"], run["eligible"]) == (3, 3, 3)


def test_customer_account_prospects_are_skipped_and_house_prospects_are_linked_not_overwritten(db, setup):
    user, _ = setup
    account, _ = accounts.create(db, "org_a", "A")
    account_only = accounts.add_prospect(db, account["id"], shop(0))
    house = db.upsert_prospect(Prospect(name="Shop 1", city="Austin", state="TX", source="irs_bmf",
                                        website_url="https://keep.test"))
    plugin = FakeSearch(count=3)
    start(db, user, CITY, max_leads=10, available={"city": plugin})
    [run] = finish(db, {"city": plugin})
    leads = generator.lead_ids(db, run["id"])
    assert account_only not in leads and house in leads and run["found"] == 2
    kept = db.get_prospect(house)
    assert (kept.source, kept.website_url) == ("irs_bmf", "https://keep.test")
    assert db.conn.execute("SELECT account_id FROM prospects WHERE id = ?",
                           (account_only,)).fetchone()[0] == account["id"]


def test_one_failing_search_is_reported_and_all_failing_fails_the_run(db, setup):
    user, _ = setup
    both = {"city": FakeSearch(count=2), "rss": Failing()}
    start(db, user, CITY + [{"type": "rss", "params": {}}], available=both)
    [run] = finish(db, both)
    assert run["status"] == "done" and run["found"] == 2
    assert run["error"] == "Some searches had problems: rss: The map data service is busy."

    start(db, user, [{"type": "rss", "params": {}}], available=both)
    [failed] = finish(db, both)
    assert (failed["status"], failed["found"]) == ("failed", 0)


def test_a_search_that_crashes_doesnt_stop_the_others(db, setup):
    user, _ = setup

    class Crashing(Failing):
        def run(self, params, ctx):
            raise RuntimeError("500 from the map server")
            yield  # noqa: unreachable
    both = {"city": Crashing(), "rss": FakeSearch(count=2)}
    start(db, user, CITY + [{"type": "rss", "params": {}}], available=both)
    [run] = finish(db, both)
    assert (run["status"], run["found"]) == ("done", 2)
    assert "city: the search failed" in run["error"] and "500" not in run["error"]


def test_cancelling_stops_a_running_run_and_a_queued_one_at_once(db, setup):
    user, _ = setup
    holder = {}
    plugin = FakeSearch(count=10, on_yield=lambda n: n == 2 and generator.cancel(db, user, holder["id"]))
    holder["id"] = start(db, user, CITY, max_leads=10, available={"city": plugin})["id"]
    [run] = finish(db, {"city": plugin})
    assert run["status"] == "canceled" and run["found"] <= 3 and run["saved_list_id"] is None

    queued = start(db, user, CITY)
    assert generator.cancel(db, user, queued["id"])["status"] == "canceled"
    assert finish(db, {"city": plugin}) == []


def test_a_run_interrupted_by_a_restart_is_failed_not_run_twice(db, setup):
    user, _ = setup
    run = start(db, user, CITY)
    db.conn.execute("""UPDATE package_runs SET status = 'running',
                         heartbeat_at = CURRENT_TIMESTAMP - INTERVAL '20 minutes' WHERE id = ?""", (run["id"],))
    assert finish(db, {"city": FakeSearch()}) == []
    assert generator.get(db, run["id"])["status"] == "failed"


def test_criteria_are_validated_before_anything_runs(db, setup):
    user, _ = setup
    with pytest.raises(generator.GeneratorError, match="at least one search"):
        start(db, user, [])
    with pytest.raises(generator.GeneratorError, match="Max leads"):
        start(db, user, CITY, max_leads=900)
    with pytest.raises(generator.GeneratorError, match="Unknown enrichers"):
        generator.create(db, user, title="x", campaign_name="gen", searches_wanted=CITY, enrichers=["nope"],
                         max_leads=5, available={"city": FakeSearch()}, known_enrichers={"fake"})
    with pytest.raises(generator.GeneratorError, match="City search: state must be"):
        generator.create(db, user, title="x", campaign_name="gen", max_leads=5, enrichers=[],
                         searches_wanted=generator.searches_from_form({"query": "dentist", "state": "XX",
                                                                       "cities": "Austin"}))


def test_the_form_becomes_one_search_per_city_and_feed():
    wanted = generator.searches_from_form({
        "query": "dentist", "state": "tx", "cities": "Austin, Round Rock", "feed_urls": "https://a.test/f\n\n",
        "keywords": "dental, clinic", "scrape_url": "https://dir.test/m", "item_selector": ".m",
        "field_name": "h3", "field_website": "a@href", "max_pages": "3"})
    assert [w["type"] for w in wanted] == ["city", "city", "rss", "scrape"]
    assert wanted[1]["params"] == {"city": "Round Rock", "state": "TX", "query": "dentist"}
    assert wanted[2]["params"]["keywords"] == ["dental", "clinic"]
    assert wanted[3]["params"]["fields"] == {"name": "h3", "website": "a@href"}
    assert wanted[3]["params"]["max_pages"] == 3


# ── Web ────────────────────────────────────────────────────────────────


def test_only_super_admins_can_use_the_generator(db, setup):
    make_user(db, "owner@x.com", access.OWNER_ROLE)
    assert client_for("owner@x.com").get("/admin/generator").status_code == 403
    assert client_for("owner@x.com").post("/admin/generator", data={}).status_code == 403
    page = client_for(SUPER).get("/admin/generator")
    assert page.status_code == 200 and "Lead package generator" in page.text
    assert "/admin/generator" not in client_for("owner@x.com").get("/admin/users").text
    assert 'href="/admin/generator"' in page.text


def test_starting_a_run_from_the_form(db, setup, monkeypatch):
    started = []
    monkeypatch.setattr(webapp, "_start_generator_run", started.append)
    db.save_campaign_file("gen/campaign.yaml", "name: gen\nproduct: u9itus_voter_guide\nprospect_sources: []\nchannels: []\n"
                          "enrichers: [local_scraper]\n")
    client = client_for(SUPER)
    bad = client.post("/admin/generator", data={"title": "x", "campaign": "gen", "max_leads": "10"})
    assert bad.status_code == 200 and "at least one search" in bad.text
    ok = client.post("/admin/generator", data={"title": "Austin dentists", "campaign": "gen", "max_leads": "25",
                                               "query": "dentist", "state": "TX", "cities": "Austin"})
    assert ok.status_code == 303 and started and ok.headers["location"] == f"/admin/generator/{started[0]}"
    run = generator.get(db, started[0])
    assert run["params"]["enrichers"] == ["local_scraper"]  # none ticked: the campaign's own
    assert "Austin dentists" in client.get(ok.headers["location"]).text
    assert db.conn.execute("SELECT COUNT(*) FROM audit_log WHERE action = 'generator.run'").fetchone()[0] == 1


def test_a_finished_run_links_to_its_leads_and_a_prefilled_publish_form(db, setup):
    user, _ = setup
    plugin = FakeSearch(count=4)
    start(db, user, CITY, available={"city": plugin})
    [run] = finish(db, {"city": plugin})
    client = client_for(SUPER)
    page = client.get(f"/admin/generator/{run['id']}").text
    assert f"/prospects?generator_run={run['id']}" in page and "Publish as package" in page
    leads = client.get(f"/prospects?generator_run={run['id']}").text
    assert "Shop 0" in leads and f"generator run #{run['id']}" in leads
    selling_page = client.get(f"/admin/selling?saved_list_id={run['saved_list_id']}&guarantee_tier=enriched"
                              "&title=Austin+shops").text
    assert f'<option value="{run["saved_list_id"]}" selected' in selling_page
    assert '<option value="enriched" selected' in selling_page and 'value="0.02"' in selling_page
