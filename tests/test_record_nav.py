"""
Moving between prospect records: the detail page asks for the previous and
next prospect in the list the user came from (same filters and sort), and never
gets one from a campaign the user can't see.

Run: TEST_DATABASE_URL=postgresql://localhost/agency_os_test python -m pytest tests/test_record_nav.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import access  # noqa: E402
from core.models import Prospect  # noqa: E402
from tests.test_access import client_for, db, make_user  # noqa: E402,F401
from tests.test_recruiting import leads  # noqa: E402,F401


def test_neighbors_follow_the_list_order_and_filters(db, leads):
    make_user(db, "owner@x.com", access.OWNER_ROLE)
    carol = db.upsert_prospect(Prospect(name="Carol Cafe", state="CA", city="Oakland", source="t"))
    c = client_for("owner@x.com")
    url = lambda pid, qs="": f"/api/prospects/{pid}/neighbors?{qs}"  # noqa: E731

    # By name: Avery Attorney, Bright Smile Dental, Carol Cafe
    mid = c.get(url(leads["dentist"])).json()
    assert mid == {"in_list": True, "prev": leads["attorney"], "next": carol, "position": 2, "total": 3}
    assert c.get(url(leads["attorney"])).json()["prev"] is None
    assert c.get(url(carol, "sort=name&dir=desc")).json()["next"] == leads["dentist"]

    oakland = c.get(url(carol, "cities=Oakland")).json()
    assert oakland["prev"] == leads["attorney"] and oakland["next"] is None and oakland["total"] == 2
    assert c.get(url(leads["dentist"], "cities=Oakland")).json() == {"in_list": False}


def test_neighbors_skip_campaigns_the_user_cannot_see(db, leads):
    make_user(db, "rep@x.com", "Sales Rep")  # no recruiting.view: the attorney is hidden
    c = client_for("rep@x.com")
    r = c.get(f"/api/prospects/{leads['dentist']}/neighbors").json()
    assert r["prev"] is None and r["total"] == 1
    assert c.get(f"/api/prospects/{leads['attorney']}/neighbors").status_code == 404


def test_detail_and_list_pages_load_the_navigation(db, leads):
    make_user(db, "owner@x.com", access.OWNER_ROLE)
    c = client_for("owner@x.com")
    page = c.get(f"/prospects/{leads['dentist']}").text
    assert f'data-record-nav="{leads["dentist"]}"' in page and "record_nav.js" in page
    assert "data-prospect-list" in c.get("/prospects").text
