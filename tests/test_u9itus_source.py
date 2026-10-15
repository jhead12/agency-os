"""
The u9itus prospect source (plugins/prospect_sources/u9itus.py): u9itus's own
businesses and organizations, read from a fake client. No network.

Run: python -m pytest tests/test_u9itus_source.py
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.campaign import CampaignConfig  # noqa: E402
from core.db import Database  # noqa: E402
from core.pipeline import Pipeline  # noqa: E402
from core.registry import PluginRegistry  # noqa: E402
from plugins.prospect_sources.u9itus import U9itusSource  # noqa: E402

BAKERY = {"id": 7, "name": "Sunrise Bakery", "slug": "sunrise-bakery", "category": "bakery",
          "profile_url": "https://www.u9itus.com/b/sunrise-bakery", "website_url": "https://sunrise.example",
          "phone": "+16265550100", "street": "1 Colorado Blvd", "city": "Pasadena", "state": "ca", "zip": "91101"}
CBO = {"id": 3, "name": "Eastside Families", "org_type": "cbo", "ein": "11-1111111", "city": "Glendale",
       "state": "CA", "contact": {"name": "Ana Ruiz", "email": "ana@eastside.example", "title": "Director"}}


class FakeClient:
    def __init__(self, pages, configured=True):
        self.pages, self.configured, self.calls = pages, configured, []

    def is_configured(self):
        return self.configured

    def prospects(self, **params):
        self.calls.append(params)
        pages = self.pages[params["kind"]]
        index = int(params.get("after") or 0)
        return pages[index] if isinstance(pages[index], dict) and "error" in pages[index] else {
            "prospects": pages[index], "next_after": str(index + 1) if index + 1 < len(pages) else None}


def test_maps_records_and_follows_the_cursor():
    client = FakeClient({"business": [[BAKERY, {"id": 8}], [{"name": "No id"}]], "organization": [[CBO]]})
    found = list(U9itusSource(client).discover({"state": "ca", "cities": ["Pasadena", "Glendale"],
                                                "u9itus_categories": "bakery, cafe"}))
    assert [p.name for p in found] == ["Sunrise Bakery", "Eastside Families"]
    bakery, cbo = found
    assert (bakery.source, bakery.state, bakery.address, bakery.focus_area) == ("u9itus", "CA", "1 Colorado Blvd", "bakery")
    assert bakery.source_url == BAKERY["profile_url"] and bakery.ein is None
    assert bakery.metadata["external_ref"] == "business:7"
    assert bakery.metadata["contact"] == {"name": None, "email": None, "phone": "+16265550100", "title": None}
    assert cbo.ein == "11-1111111" and cbo.metadata["contact"]["email"] == "ana@eastside.example"

    business_calls = [c for c in client.calls if c["kind"] == "business"]
    assert [c["after"] for c in business_calls] == [None, "1"]
    assert business_calls[0]["categories"] == "bakery,cafe" and business_calls[0]["cities"] == "Pasadena,Glendale"
    assert next(c for c in client.calls if c["kind"] == "organization")["categories"] == ""


def test_kinds_max_and_off_without_settings():
    client = FakeClient({"business": [[BAKERY, {**BAKERY, "id": 9}]], "organization": [[CBO]]})
    found = list(U9itusSource(client).discover({"u9itus_kinds": ["business"], "u9itus_max": 1}))
    assert [p.metadata["external_ref"] for p in found] == ["business:7"]
    assert {c["kind"] for c in client.calls} == {"business"}
    assert list(U9itusSource(FakeClient({}, configured=False)).discover({})) == []


def test_candidates_only_when_named_and_contact_defaults_to_the_candidate():
    candidate = {"id": 41, "name": "Lily Schworm", "office": "Chula Vista School Board, Area 2",
                 "level": "school_board", "race": "sd-cvesd-2", "election_date": "2026-11-03",
                 "county": "San Diego County", "state": "CA", "profile_url": "https://www.u9itus.com/p/lily-schworm",
                 "contact": {"email": "lily@example.org", "phone": "+16195550100"}}
    client = FakeClient({"business": [[BAKERY]], "organization": [[CBO]], "candidate": [[candidate]]})
    list(U9itusSource(client).discover({}))
    assert {c["kind"] for c in client.calls} == {"business", "organization"}

    client.calls.clear()
    (person,) = U9itusSource(client).discover({"u9itus_kinds": ["candidate"], "counties": ["San Diego County"],
                                               "u9itus_levels": "city, school_board"})
    assert client.calls[0]["levels"] == "city,school_board" and client.calls[0]["counties"] == "San Diego County"
    assert person.metadata["external_ref"] == "candidate:41" and person.focus_area == candidate["office"]
    assert person.county == "San Diego County" and person.ein is None
    assert person.metadata["contact"]["name"] == "Lily Schworm"
    assert person.metadata["u9itus"]["race"] == "sd-cvesd-2"


def test_an_error_from_u9itus_stops_the_sync():
    client = FakeClient({"business": [{"error": True, "status": 401, "detail": "bad token"}]})
    with pytest.raises(ValueError, match="401"):
        list(U9itusSource(client).discover({"u9itus_kinds": "business"}))


def test_sync_seeds_the_contact_and_matches_on_resync(pg_url, tmp_path):
    db = Database(pg_url)
    registry = PluginRegistry()
    registry.sources["u9itus"] = U9itusSource(FakeClient({"business": [[BAKERY]], "organization": [[CBO]]}))
    campaign = CampaignConfig(name="Hybrid", product="none", prospect_sources=["u9itus"], channels=[],
                              config_dir=tmp_path)
    pipeline = Pipeline(db, registry)
    assert pipeline.sync_prospects(campaign)["upserted"] == 2
    pipeline.sync_prospects(campaign)
    rows = db.conn.execute(
        "SELECT p.name, o.contact_phone, o.contact_email FROM outreach o JOIN prospects p ON p.id = o.prospect_id "
        "ORDER BY p.name").fetchall()
    assert [tuple(r) for r in rows] == [("Eastside Families", None, "ana@eastside.example"),
                                        ("Sunrise Bakery", "+16265550100", None)]
