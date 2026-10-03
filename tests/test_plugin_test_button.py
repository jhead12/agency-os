"""
Tests for the u9itus "Check connection" / "Create test portal" buttons on the
Plugins page. The u9itus client is faked; nothing calls the network.

Run: python -m pytest tests/
"""

import sys
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from plugins.products import u9itus_client  # noqa: E402
from tests.test_access import client_for, db, make_user  # noqa: E402,F401


class FakeClient:
    calls = []
    response = {}

    def __init__(self, *args, **kwargs):
        pass

    def is_configured(self):
        return True

    def pull_events(self, after=0, limit=100):
        FakeClient.calls.append(("events", after, limit))
        return FakeClient.response or {"events": [], "next_cursor": 0}

    def provision_demo(self, **kwargs):
        FakeClient.calls.append(("create", kwargs))
        return FakeClient.response or {
            "slug": "test-portal-1", "status": "demo",
            "demo_url": "https://u9.example/portal/test-portal-1?src=outreach&t=abc",
            "claim_url": "https://u9.example/portal/test-portal-1/claim?t=abc",
        }

    def close(self):
        pass


@pytest.fixture
def fake_client(monkeypatch):
    FakeClient.calls = []
    FakeClient.response = {}
    monkeypatch.setattr(u9itus_client, "U9itusClient", FakeClient)
    return FakeClient


def test_plugins_page_shows_test_buttons_to_portal_managers(db, fake_client, monkeypatch):
    monkeypatch.setenv("U9ITUS_BASE_URL", "https://u9.example")
    monkeypatch.setenv("U9ITUS_AGENCY_TOKEN", "t")
    make_user(db, "rep@agency.example", "Sales Rep")
    make_user(db, "viewer@agency.example", "Viewer")

    assert "Create test portal" in client_for("rep@agency.example").get("/plugins").text
    assert "Create test portal" not in client_for("viewer@agency.example").get("/plugins").text


def test_check_connection_reads_one_event_and_creates_nothing(db, fake_client):
    make_user(db, "rep@agency.example", "Sales Rep")
    r = client_for("rep@agency.example").post("/plugins/u9itus_voter_guide/test", data={"action": "check"})
    assert r.status_code == 303 and "connection%20OK" in r.headers["location"]
    assert fake_client.calls == [("events", 0, 1)]


def test_create_makes_a_throwaway_portal_and_shows_its_links(db, fake_client):
    make_user(db, "rep@agency.example", "Sales Rep")
    client = client_for("rep@agency.example")
    r = client.post("/plugins/u9itus_voter_guide/test", data={"action": "create"})
    assert r.status_code == 303

    (kind, kwargs), = fake_client.calls
    assert kind == "create" and kwargs["external_ref"].startswith("agency-os:test:")
    assert kwargs["state"] == "CA"

    page = client.get(r.headers["location"]).text
    assert "test-portal-1" in page and "Open demo page" in page and "Open claim page" in page


@pytest.mark.parametrize("status,hint", [(401, "token rejected"), (404, "not deployed"),
                                         (503, "AGENCY_OS_TOKEN_HASH")])
def test_api_errors_are_explained(db, fake_client, status, hint):
    fake_client.response = {"error": True, "status": status, "detail": "x"}
    make_user(db, "rep@agency.example", "Sales Rep")
    r = client_for("rep@agency.example").post("/plugins/u9itus_voter_guide/test", data={"action": "check"})
    error = parse_qs(urlsplit(r.headers["location"]).query)["error"][0]
    assert f"({status})" in error and hint in error


def test_viewer_cannot_create_a_test_portal(db, fake_client):
    make_user(db, "viewer@agency.example", "Viewer")
    r = client_for("viewer@agency.example").post("/plugins/u9itus_voter_guide/test", data={"action": "create"})
    assert r.status_code in (302, 303, 403)
    assert fake_client.calls == []
