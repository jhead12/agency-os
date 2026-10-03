"""
Browser tests for the grouped navigation (web/templates/base.html, web/static/app.js).

Needs Playwright and a test database:

    pip install pytest-playwright && python -m playwright install chromium
    TEST_DATABASE_URL=postgresql://localhost/agency_os_test python -m pytest tests/test_nav_e2e.py

Skipped when Playwright isn't installed or TEST_DATABASE_URL isn't set.
"""

import socket
import threading
import time

import pytest

pytest.importorskip("playwright")
import uvicorn

from core import access
from core.db import Database
import web.app as webapp

EMAIL = "owner@test.com"
PASSWORD = "correct-horse-battery"
DESKTOP = {"width": 1280, "height": 800}
MOBILE = {"width": 390, "height": 844}


@pytest.fixture
def app_url(pg_url, monkeypatch):
    """Run the dashboard on a free port against the emptied test database."""
    monkeypatch.setattr(webapp, "DB_URL", pg_url)
    for var in ("AGENCY_OS_OWNER_EMAIL", "AGENCY_OS_OWNER_PASSWORD", "AGENCY_OS_RUN_JOBS"):
        monkeypatch.delenv(var, raising=False)
    db = Database(pg_url)
    db.install_access()
    owner = next(r["id"] for r in db.list_roles() if r["name"] == access.OWNER_ROLE)
    db.create_user(EMAIL, "Test Owner", PASSWORD, [owner], actor=None)

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(webapp.app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 10
    while not server.started:
        if time.time() > deadline:
            pytest.fail("dashboard did not start")
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(timeout=5)


def sign_in(page, app_url, next_path="/"):
    page.goto(f"{app_url}/login?next={next_path}")
    page.fill("input[name=email]", EMAIL)
    page.fill("input[name=password]", PASSWORD)
    page.click("button[type=submit]")
    page.wait_for_url(f"{app_url}{next_path}")


def group(page, label):
    return page.locator("details.nav-group", has=page.locator("summary", has_text=label))


def is_open(locator):
    return locator.evaluate("el => el.open")


def test_login_page_has_no_menu_toggle(app_url, page):
    page.set_viewport_size(MOBILE)
    page.goto(f"{app_url}/login")
    assert page.locator("#nav-toggle").count() == 0


def test_account_menu_is_labelled_and_names_user(app_url, page):
    page.set_viewport_size(DESKTOP)
    sign_in(page, app_url)
    account = page.locator("details.nav-account")
    assert account.locator("summary").inner_text().strip() == "Account"
    account.locator("summary").click()
    assert "Test Owner" in account.locator(".nav-account-id").inner_text()
    assert account.locator(".nav-account-id").get_attribute("title") == EMAIL


@pytest.mark.parametrize("path,expected", [
    ("/", "dashboard"), ("/prospects", "prospects"), ("/prospects/12", "prospects"),
    ("/call-log", "call-log"), ("/admin/users", "admin"), ("/admin/roles", "admin"),
    ("/admin/campaigns", "admin-campaigns"), ("/admin/campaigns/voter-guide", "admin-campaigns"),
])
def test_nav_active_from_path(path, expected):
    assert webapp.nav_active(path) == expected


def test_current_page_is_highlighted(app_url, page):
    page.set_viewport_size(DESKTOP)
    sign_in(page, app_url)
    for path, group_label in [("/", None), ("/prospects", None), ("/call-log", "Outreach"),
                              ("/calendar", "Outreach"), ("/campaigns", "Campaigns"),
                              ("/admin/campaigns", "Administration")]:
        page.goto(f"{app_url}{path}")
        assert page.locator("#nav-links a.active").get_attribute("href") == path, path
        active_groups = page.locator("details.nav-group-active summary")
        if group_label:
            assert active_groups.inner_text().strip() == group_label, path
        else:
            assert active_groups.count() == 0, path


def test_desktop_dropdown_closes_when_focus_leaves(app_url, page):
    page.set_viewport_size(DESKTOP)
    sign_in(page, app_url)
    outreach = group(page, "Outreach")
    outreach.locator("summary").click()
    assert is_open(outreach)
    outreach.locator(".nav-dropdown a").last.focus()
    page.keyboard.press("Tab")
    assert not is_open(outreach)


def test_desktop_only_one_group_open_and_outside_click_closes(app_url, page):
    page.set_viewport_size(DESKTOP)
    sign_in(page, app_url)
    outreach, campaigns = group(page, "Outreach"), group(page, "Campaigns")
    outreach.locator("summary").click()
    campaigns.locator("summary").click()
    assert not is_open(outreach) and is_open(campaigns)
    page.mouse.click(640, 600)
    assert not is_open(campaigns)


def test_mobile_menu_opens_current_section(app_url, page):
    page.set_viewport_size(MOBILE)
    sign_in(page, app_url, "/call-log")
    toggle = page.locator("#nav-toggle")
    toggle.click()
    assert page.locator("#nav-links").is_visible()
    assert toggle.get_attribute("aria-expanded") == "true"
    assert is_open(group(page, "Outreach"))
    assert not is_open(group(page, "Campaigns"))


def test_mobile_menu_closes_on_outside_tap(app_url, page):
    page.set_viewport_size(MOBILE)
    sign_in(page, app_url, "/call-log")
    page.locator("#nav-toggle").click()
    box = page.locator("#nav-links").bounding_box()
    assert box["y"] + box["height"] < MOBILE["height"] - 20, "menu fills the screen; no room to tap outside"
    page.mouse.click(MOBILE["width"] / 2, MOBILE["height"] - 10)
    assert not page.locator("#nav-links").is_visible()
    assert page.locator("#nav-toggle").get_attribute("aria-expanded") == "false"


def test_mobile_menu_closes_on_escape_and_refocuses_toggle(app_url, page):
    page.set_viewport_size(MOBILE)
    sign_in(page, app_url, "/call-log")
    page.locator("#nav-toggle").click()
    page.keyboard.press("Escape")
    assert not page.locator("#nav-links").is_visible()
    assert page.evaluate("document.activeElement.id") == "nav-toggle"
