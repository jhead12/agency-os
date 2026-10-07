"""
Browser tests for opening the console from the Account page (web/static/console.js, player.js).

    TEST_DATABASE_URL=postgresql://localhost/agency_os_test python -m pytest tests/test_console_e2e.py

Skipped when Playwright isn't installed or TEST_DATABASE_URL isn't set.
"""

import pytest

pytest.importorskip("playwright")

from tests.test_nav_e2e import DESKTOP, app_url, sign_in  # noqa: F401


def test_show_me_how_plays_the_console_tutorial_in_the_floating_window(app_url, page):  # noqa: F811
    page.set_viewport_size(DESKTOP)
    sign_in(page, app_url, "/account")
    page.click("text=Show me how")
    page.locator(".player-bar").wait_for()
    assert "Use the console" in page.locator(".player-title").inner_text()
    page.click(".player-bar >> text=Next")  # step 2 runs help
    page.locator("#console-dock .console-output", has_text="help").wait_for()
    assert page.locator("#console-dock").is_visible() and page.url == f"{app_url}/account"


def test_open_the_console_uses_the_floating_window(app_url, page):  # noqa: F811
    page.set_viewport_size(DESKTOP)
    sign_in(page, app_url, "/account")
    page.click("#cli >> text=Open the console")
    assert page.locator("#console-dock").is_visible() and page.url == f"{app_url}/account"
    assert page.locator("#console-dock-input").evaluate("el => el === document.activeElement")
