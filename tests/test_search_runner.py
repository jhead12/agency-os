"""
Running paid searches (core/searches.py SearchRunner) and the city search
(plugins/searches/city.py). Overpass is faked with httpx.MockTransport; nothing
touches the network.

Run: TEST_DATABASE_URL=postgresql://localhost/agency_os_test python -m pytest tests/test_search_runner.py
"""

import json
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import accounts, searches  # noqa: E402
from core.models import Prospect  # noqa: E402
from plugins.searches.city import CitySearch, build_query  # noqa: E402
from tests.test_access import client_for, db  # noqa: E402,F401


def shop(n):
    return Prospect(name=f"Shop {n}", city="Austin", state="TX", source="fake")


class FakeSearch:
    """Yields `count` shops, raising `fail` after `fail_after` of them."""

    def __init__(self, count=10, fail=None, fail_after=0, on_yield=None):
        self.count, self.fail, self.fail_after, self.on_yield = count, fail, fail_after, on_yield

    def validate(self, params):
        return params

    def run(self, params, ctx):
        for n in range(self.count):
            if self.fail and n == self.fail_after:
                raise self.fail
            ctx.count(api_requests=1, cost_cents=2)
            if self.on_yield:
                self.on_yield(n)
            yield shop(n)


@pytest.fixture
def account(db):
    found, key = accounts.create(db, "org_a", "A")
    return found


def queue(db, account, max_results=3, key="s1", kind="city"):
    body = {"type": kind, "params": {"city": "Austin", "state": "TX", "query": "dentist"},
            "max_results": max_results, "idempotency_key": key}
    return searches.create(db, account["id"], body)[0]


def run(db, plugin, kind="city"):
    return searches.SearchRunner(db.url, available={kind: plugin}).run_pending()


# ── Runner ─────────────────────────────────────────────────────────────


def test_a_search_delivers_up_to_max_results_and_records_usage(db, account):
    queue(db, account, max_results=3)
    [done] = run(db, FakeSearch(count=10))
    assert (done["status"], done["delivered"], done["error"]) == ("done", 3, None)
    assert (done["api_requests"], done["cost_cents"]) == (3, 6)
    assert done["started_at"] and done["finished_at"]
    assert len(accounts.prospects(db, account["id"])) == 3
    assert run(db, FakeSearch()) == []  # nothing left queued


def test_prospects_the_account_already_had_are_listed_but_not_billed(db, account):
    accounts.add_prospect(db, account["id"], shop(0))
    queue(db, account, max_results=3)
    [done] = run(db, FakeSearch(count=10))
    assert done["delivered"] == 3
    assert [p["name"] for p in accounts.prospects(db, account["id"], search_id=done["id"])] == \
        ["Shop 0", "Shop 1", "Shop 2", "Shop 3"]


def test_a_plugin_error_fails_the_search_but_keeps_what_it_found(db, account, capsys):
    queue(db, account, max_results=5, key="a")
    queue(db, account, max_results=5, key="b")
    plugin = FakeSearch(count=5, fail=ValueError("Nothing found in Austin."), fail_after=2)
    first, second = run(db, plugin)
    assert (first["status"], first["delivered"], first["error"]) == ("failed", 2, "Nothing found in Austin.")
    # Anything else is a bug or outage: the customer gets a plain message, the log gets the details.
    db.conn.execute("UPDATE searches SET status = 'queued', delivered = 0 WHERE id = ?", (second["id"],))
    [crashed] = run(db, FakeSearch(count=5, fail=RuntimeError("socket closed"), fail_after=1))
    assert crashed["status"] == "failed" and "socket" not in crashed["error"]
    assert "socket closed" in capsys.readouterr().out


def test_cancelling_a_running_search_stops_it(db, account):
    search = queue(db, account, max_results=10)
    cancel = lambda n: n == 2 and searches.cancel(db, account["id"], search["id"])  # noqa: E731
    [done] = run(db, FakeSearch(count=10, on_yield=cancel))
    assert (done["status"], done["delivered"]) == ("canceled", 2)


def test_a_type_without_a_plugin_fails(db, account):
    queue(db, account, kind="city")
    [done] = searches.SearchRunner(db.url, available={}).run_pending()
    assert done["status"] == "failed" and "aren't available" in done["error"]


def test_suspended_accounts_and_busy_accounts_wait(db, account):
    other, _ = accounts.create(db, "org_b", "B")
    for key in ("a", "b", "c"):
        queue(db, account, key=key)
    queue(db, other, key="x")
    runner = searches.SearchRunner(db.url, available={"city": FakeSearch()})
    first, second = runner.claim(db), runner.claim(db)
    assert {first["account_id"], second["account_id"]} == {account["id"]}
    third = runner.claim(db)  # org_a already has two running
    assert third["account_id"] == other["id"]
    assert runner.claim(db) is None
    accounts.set_status(db, "org_a", "suspended")
    db.conn.execute("UPDATE searches SET status = 'done'")
    queue(db, account, key="d")
    assert runner.claim(db) is None


def test_a_search_interrupted_by_a_restart_fails_instead_of_running_twice(db, account):
    search = queue(db, account)
    db.conn.execute("""UPDATE searches SET status = 'running', delivered = 1,
                       heartbeat_at = CURRENT_TIMESTAMP - INTERVAL '11 minutes' WHERE id = ?""", (search["id"],))
    assert run(db, FakeSearch()) == []
    row = searches.get(db, account["id"], search["id"])
    assert (row["status"], row["delivered"]) == ("failed", 1) and "interrupted" in row["error"]


def test_the_api_refuses_types_without_a_plugin_and_validates_params(db, account, monkeypatch):
    monkeypatch.setattr(searches, "_plugins", {"city": CitySearch()})
    _, key = accounts.create(db, "org_c", "C")
    auth = {"Authorization": f"Bearer {key}"}
    c = client_for()
    rss = {"type": "rss", "params": {"feed_url": "https://example.com/feed"}, "max_results": 5,
           "idempotency_key": "r1"}
    r = c.post("/api/v1/searches", json=rss, headers=auth)
    assert r.status_code == 422 and "aren't available yet" in r.json()["message"]
    bad = {"type": "city", "params": {"city": "Austin", "state": "ZZ", "query": "dentist"}, "max_results": 5,
           "idempotency_key": "c1"}
    assert c.post("/api/v1/searches", json=bad, headers=auth).status_code == 422
    messy = {**bad, "params": {"city": "  Austin ", "state": "tx", "query": "dentist"}}
    r = c.post("/api/v1/searches", json=messy, headers=auth)
    assert r.status_code == 202 and r.json()["search"]["params"] == {"city": "Austin", "state": "TX",
                                                                      "query": "dentist"}


# ── City search ────────────────────────────────────────────────────────

ELEMENTS = [
    {"type": "node", "id": 1, "tags": {"name": "Bright Smiles", "amenity": "dentist", "website": "https://bs.example",
                                       "addr:housenumber": "100", "addr:street": "Congress Ave",
                                       "addr:postcode": "78701", "phone": "+1 512 555 0100"}},
    {"type": "way", "id": 2, "center": {"lat": 30.2, "lon": -97.7}, "tags": {"name": "Austin Dental Care",
                                                                             "healthcare": "dentist"}},
    {"type": "node", "id": 3, "tags": {"amenity": "dentist"}},   # no name: skipped
]


def overpass(elements=ELEMENTS, status=200, seen=None, remark=None):
    def handler(request):
        if seen is not None:
            seen.append(request)
        return httpx.Response(status, json={"elements": elements, **({"remark": remark} if remark else {})})
    return httpx.MockTransport(handler)


def run_city(db, account, transport, key="c1", max_results=10):
    body = {"type": "city", "params": {"city": "Austin", "state": "TX", "query": "Dentists"},
            "max_results": max_results, "idempotency_key": key}
    searches.create(db, account["id"], body, {"city": CitySearch()})
    return searches.SearchRunner(db.url, available={"city": CitySearch()}, transport=transport).run_pending()[0]


@pytest.mark.parametrize("params", [
    {"city": "Austin", "state": "Texas", "query": "dentist"},
    {"city": 'Austin"];out;', "state": "TX", "query": "dentist"},
    {"city": "Austin", "state": "TX", "query": 'dentist"]'},
    {"city": "Austin", "state": "TX", "query": "x"},
])
def test_city_params_are_checked(params):
    with pytest.raises(ValueError):
        CitySearch().validate(params)


def test_city_query_asks_for_the_category_and_names_in_the_city():
    q = build_query("Austin", "TX", "Dentists", 50)
    assert '["ISO3166-2"="US-TX"]' in q and '["name"="Austin"]' in q
    assert '["amenity"="dentist"]' in q and '["healthcare"="dentist"]' in q
    assert '["name"~"Dentists",i]' in q and "out tags center 50;" in q
    assert '["name"~"Joe.s Bar . Grill",i]' in build_query("Austin", "TX", "Joe's Bar & Grill", 5)


def test_a_city_search_saves_named_places_once(db, account):
    seen = []
    done = run_city(db, account, overpass(seen=seen))
    assert (done["status"], done["delivered"], done["api_requests"]) == ("done", 2, 1)
    assert b"US-TX" in seen[0].content and seen[0].headers["user-agent"].startswith("agency-os")
    found = {p["name"]: p for p in accounts.prospects(db, account["id"])}
    assert set(found) == {"Bright Smiles", "Austin Dental Care"}
    smile = found["Bright Smiles"]
    assert (smile["address"], smile["zip"], smile["website_url"], smile["focus_area"]) == \
        ("100 Congress Ave", "78701", "https://bs.example", "dentist")
    assert smile["source_url"] == "https://www.openstreetmap.org/node/1"
    # The same places found again aren't new, so aren't billed again.
    again = run_city(db, account, overpass(), key="c2")
    assert (again["status"], again["delivered"]) == ("done", 0)
    assert db.conn.execute("SELECT COUNT(*) FROM prospects").fetchone()[0] == 2


def test_city_search_failures_explain_themselves(db, account):
    busy = run_city(db, account, overpass(status=429), key="busy")
    assert busy["status"] == "failed" and "busy" in busy["error"]
    timed_out = run_city(db, account, overpass(elements=[], remark="runtime error: Query timed out in \"query\""),
                         key="timeout")
    assert timed_out["status"] == "failed" and "busy" in timed_out["error"]
    empty = run_city(db, account, overpass(elements=[]), key="empty")
    assert empty["status"] == "failed" and "Nothing found" in empty["error"]
    broken = run_city(db, account, overpass(status=500), key="broken")
    assert broken["status"] == "failed" and broken["error"].startswith("The search failed")
    assert json.loads(searches.get(db, account["id"], broken["id"])["params"])["state"] == "TX"


# ── scrape and rss searches (plugins/searches/scrape.py, rss.py) ────────

from plugins.searches.rss import RssSearch  # noqa: E402
from plugins.searches.scrape import ScrapeSearch  # noqa: E402


@pytest.fixture
def public_dns(monkeypatch):
    """Every host resolves to one public address, so ctx.fetch's checks pass offline."""
    import socket

    monkeypatch.setattr(socket, "getaddrinfo", lambda host, port, **_: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))])
    monkeypatch.setattr("core.safe_fetch.MIN_INTERVAL_SECONDS", 0)


def serve(pages: dict) -> httpx.MockTransport:
    """{(host, path): (body, content_type)}; robots.txt and anything else is 404."""
    def handler(request):
        found = pages.get((request.headers["host"], request.url.path))
        if found is None:
            return httpx.Response(404)
        body, kind = found
        return httpx.Response(200, content=body.encode(), headers={"content-type": kind})
    return httpx.MockTransport(handler)


def queue_type(db, account, kind, params, max_results=10, key="t1"):
    body = {"type": kind, "params": params, "max_results": max_results, "idempotency_key": key}
    return searches.create(db, account["id"], body, {kind: {"scrape": ScrapeSearch, "rss": RssSearch}[kind]()})[0]


def run_with(db, kind, pages):
    plugin = {"scrape": ScrapeSearch, "rss": RssSearch}[kind]()
    return searches.SearchRunner(db.url, available={kind: plugin}, transport=serve(pages)).run_pending()


MEMBERS = """<html><body>{items}{next}</body></html>"""


def member(name, site):
    return f'<div class="m"><h3>{name}</h3><a class="site" href="{site}">site</a><span class="tel">512-555-0100</span></div>'


SCRAPE = {"url": "https://chamber.test/members", "item_selector": ".m",
          "fields": {"name": "h3", "website": "a.site@href", "phone": ".tel"}, "next_selector": "a.next@href"}


def test_scrape_follows_next_pages_on_the_same_host_only(db, account, public_dns):
    page_one = MEMBERS.format(items=member("Acme Dental", "https://acme.test") + member("Bright Smiles", "/b"),
                              next='<a class="next" href="/p2">Next</a>')
    page_two = MEMBERS.format(items=member("Cedar Clinic", "https://cedar.test"),
                              next='<a class="next" href="https://elsewhere.test/p3">Next</a>')
    pages = {("chamber.test", "/members"): (page_one, "text/html"), ("chamber.test", "/p2"): (page_two, "text/html"),
             ("elsewhere.test", "/p3"): (MEMBERS.format(items=member("Off Host", "x"), next=""), "text/html")}
    queue_type(db, account, "scrape", SCRAPE)
    [done] = run_with(db, "scrape", pages)
    assert (done["status"], done["delivered"], done["pages_fetched"]) == ("done", 3, 2)
    found = {p["name"]: p for p in accounts.prospects(db, account["id"])}
    assert set(found) == {"Acme Dental", "Bright Smiles", "Cedar Clinic"}
    assert found["Bright Smiles"]["website_url"] == "https://chamber.test/b"
    assert found["Acme Dental"]["source"] == "scrape"


def test_scrape_stops_at_max_pages_and_explains_selectors_that_match_nothing(db, account, public_dns):
    page_one = MEMBERS.format(items=member("Acme Dental", "https://acme.test"), next='<a class="next" href="/p2">n</a>')
    pages = {("chamber.test", "/members"): (page_one, "text/html"),
             ("chamber.test", "/p2"): (MEMBERS.format(items=member("Two", "x"), next=""), "text/html")}
    queue_type(db, account, "scrape", {**SCRAPE, "max_pages": 1})
    [done] = run_with(db, "scrape", pages)
    assert (done["delivered"], done["pages_fetched"]) == (1, 1)

    queue_type(db, account, "scrape", {**SCRAPE, "item_selector": ".nothing"}, key="t2")
    [failed] = run_with(db, "scrape", pages)
    assert failed["status"] == "failed" and "No organizations matched" in failed["error"]


def test_scrape_validates_its_settings():
    with pytest.raises(ValueError, match="include name"):
        ScrapeSearch().validate({**SCRAPE, "fields": {"website": "a@href"}})
    with pytest.raises(ValueError, match="fields can only be"):
        ScrapeSearch().validate({**SCRAPE, "fields": {"name": "h3", "email": ".mail"}})
    with pytest.raises(ValueError, match="max_pages"):
        ScrapeSearch().validate({**SCRAPE, "max_pages": 50})
    with pytest.raises(ValueError, match="url must be"):
        ScrapeSearch().validate({**SCRAPE, "url": "javascript:alert(1)"})


RSS = """<?xml version="1.0"?><rss version="2.0"><channel><title>Austin news</title>
<item><title>New dental clinic opens</title><link>https://news.test/a</link>
  <source url="https://acme-dental.test/feed">Acme Dental</source><pubDate>Mon, 05 Oct 2026 10:00:00 GMT</pubDate></item>
<item><title>Bakery wins award</title><link>https://bakery.test/award</link></item>
<item><title>Dental school expands</title><link>https://www.school.test/news</link>
  <pubDate>Mon, 01 Jun 2026 10:00:00 GMT</pubDate></item>
<item><title>Another dental story</title><link>https://news.test/b</link>
  <source url="https://acme-dental.test/">Acme Dental</source></item>
</channel></rss>"""

ATOM = """<?xml version="1.0" encoding="utf-8"?><feed xmlns="http://www.w3.org/2005/Atom"><title>Feed</title>
<entry><title>Cedar Clinic hires</title><link href="https://cedar.test/post"/><summary>dental care</summary>
  <updated>2026-10-01T00:00:00Z</updated></entry>
</feed>"""


def test_rss_finds_the_organizations_behind_matching_items(db, account, public_dns):
    queue_type(db, account, "rss", {"feed_url": "https://news.test/feed", "keywords": "dental"})
    [done] = run_with(db, "rss", {("news.test", "/feed"): (RSS, "application/rss+xml")})
    assert (done["status"], done["delivered"]) == ("done", 2)  # Acme once, plus the school; not the bakery
    found = {p["name"]: p for p in accounts.prospects(db, account["id"])}
    assert set(found) == {"Acme Dental", "school.test"}
    assert found["Acme Dental"]["website_url"] == "https://acme-dental.test"

    queue_type(db, account, "rss", {"feed_url": "https://news.test/feed", "keywords": ["dental"],
                                    "since": "2026-09-01"}, key="t2")
    [recent] = run_with(db, "rss", {("news.test", "/feed"): (RSS, "application/rss+xml")})
    assert recent["delivered"] == 0  # Acme was already the account's; the school's item is too old


def test_rss_reads_atom_and_fails_clearly_on_anything_else(db, account, public_dns):
    queue_type(db, account, "rss", {"feed_url": "https://cedar.test/atom"})
    [done] = run_with(db, "rss", {("cedar.test", "/atom"): (ATOM, "application/atom+xml")})
    assert done["delivered"] == 1
    for n, body in enumerate(["<html>not a feed</html>", "<?xml version='1.0'?><!DOCTYPE x [<!ENTITY a 'b'>]><rss/>",
                              "not xml at all"]):
        queue_type(db, account, "rss", {"feed_url": "https://bad.test/feed"}, key=f"bad{n}")
        [failed] = run_with(db, "rss", {("bad.test", "/feed"): (body, "text/xml")})
        assert (failed["status"], failed["error"]) == ("failed", "That feed couldn't be read as RSS or Atom.")


def test_a_feed_on_an_internal_address_fails_without_being_fetched(db, account, monkeypatch):
    import socket

    monkeypatch.setattr(socket, "getaddrinfo", lambda host, port, **_: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.2", port))])
    queue_type(db, account, "rss", {"feed_url": "http://internal.test/feed"})
    [failed] = run_with(db, "rss", {("internal.test", "/feed"): (RSS, "text/xml")})
    assert failed["status"] == "failed" and "private or internal" in failed["error"]
