"""
Tap-to-call: phone numbers become tel: links that hand the call to the
user's phone (phone app, "Call from iPhone" on a Mac, Phone Link on Windows).

Run: TEST_DATABASE_URL=postgresql://localhost/agency_os_test python -m pytest tests/test_tap_to_call.py
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from web.app import tel_href  # noqa: E402
from core.models import Prospect  # noqa: E402
from tests.test_access import client_for, db, make_user  # noqa: E402,F401


@pytest.mark.parametrize("phone, href", [
    ("(213) 555-0100", "tel:+12135550100"),
    ("213.555.0100", "tel:+12135550100"),
    ("1-213-555-0100", "tel:+12135550100"),
    ("+44 20 7946 0958", "tel:+442079460958"),
    ("555-0100", ""),             # no area code: not dialable on its own
    ("call the front desk", ""),
    ("", ""),
    (None, ""),
])
def test_tel_href(phone, href):
    assert tel_href(phone) == href


def test_prospect_and_script_pages_link_the_phone(db):
    make_user(db, "rep@x.com", "Sales Rep")
    campaign_id = db.upsert_campaign("voter-guide--cbo-outreach-los-angeles", "x")
    pid = db.upsert_prospect(Prospect(name="Civic Org", state="CA", city="Los Angeles"))
    oid = db.upsert_outreach(pid, campaign_id)
    db.update_outreach(oid, {"contact_name": "Dana Lee", "contact_phone": "(213) 555-0100"})
    client = client_for("rep@x.com")

    page = client.get(f"/prospects/{pid}").text
    assert 'href="tel:+12135550100"' in page and "Call Dana Lee" in page
    assert f'data-call-log="#log-call-{oid}"' in page and f'id="log-call-{oid}"' in page

    scripts = client.get(f"/call-scripts?prospect_id={pid}").text
    assert 'href="tel:+12135550100"' in scripts

    db.update_outreach(oid, {"contact_phone": "ask reception"})
    assert "tel:" not in client.get(f"/prospects/{pid}").text


def test_enriched_website_and_summary_reach_the_call_scripts(db):
    from core.models import EnrichmentResult
    make_user(db, "rep@x.com", "Sales Rep")
    campaign_id = db.upsert_campaign("voter-guide--cbo-outreach-los-angeles", "x")
    pid = db.upsert_prospect(Prospect(name="Civic Org", state="CA", city="Los Angeles"))
    oid = db.upsert_outreach(pid, campaign_id)
    client = client_for("rep@x.com")

    scripts = client.get(f"/call-scripts?prospect_id={pid}").text
    assert "no site summary yet" in scripts and "From their website" not in scripts

    db.apply_enrichment(oid, EnrichmentResult(
        contact_name="Dana Lee", contact_title="Executive Director",
        raw={"website": "https://civic.example.org", "site_summary": "We register first-time voters."},
    ), prospect_id=pid)
    assert db.get_prospect(pid).site_summary == "We register first-time voters."

    scripts = client.get(f"/call-scripts?prospect_id={pid}").text
    assert "From their website:</strong> We register first-time voters." in scripts
    assert "[Contact: Dana Lee, Executive Director]" in scripts
    assert "[Website: https://civic.example.org]" in scripts
