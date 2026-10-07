"""
Browser tests for the chat robot (web/static/agent_chat.js). The model is faked.

    TEST_DATABASE_URL=postgresql://localhost/agency_os_test python -m pytest tests/test_agent_chat_e2e.py

Skipped when Playwright isn't installed or TEST_DATABASE_URL isn't set.
"""

import pytest

pytest.importorskip("playwright")

from core import llm
from core.db import Database
from core.models import Prospect
from tests.test_nav_e2e import DESKTOP, EMAIL, MOBILE, app_url, sign_in  # noqa: F401


@pytest.fixture
def chat_app(app_url, pg_url, monkeypatch):  # noqa: F811
    """The dashboard with AI on for the owner, one prospect, and a model that answers by number."""
    monkeypatch.delenv("AGENCY_OS_AI", raising=False)
    calls = []

    def chat(system, messages, **kw):
        calls.append({"system": system, "messages": messages})
        return llm.Reply(True, text=f"Answer {len(calls)}")

    monkeypatch.setattr(llm, "backend", lambda: "anthropic")
    monkeypatch.setattr(llm, "chat", chat)
    db = Database(pg_url)
    user_id = db.conn.execute("SELECT id FROM users WHERE email = ?", (EMAIL,)).fetchone()["id"]
    db.set_ai_enabled(user_id, True, actor=None)
    campaign_id = db.upsert_campaign("chat-test", "x")
    prospect_id = db.upsert_prospect(Prospect(name="Civic Org", state="CA", city="Los Angeles"))
    db.upsert_outreach(prospect_id, campaign_id)
    return app_url, prospect_id, calls


def test_chat_on_a_prospect_page_remembers_the_conversation(chat_app, page):
    url, prospect_id, calls = chat_app
    page.set_viewport_size(DESKTOP)
    sign_in(page, url, f"/prospects/{prospect_id}")
    page.click("#chat-launcher")
    assert page.locator("#chat-dock").is_visible()
    assert "Civic Org" in page.locator(".chat-intro").inner_text()

    page.fill("#chat-input", "What do we know?")
    page.press("#chat-input", "Enter")
    page.locator(".chat-msg-assistant .chat-text", has_text="Answer 1").wait_for()
    assert "Civic Org" in calls[0]["system"]
    page.fill("#chat-input", "And next?")
    page.click("#chat-form button[type=submit]")
    page.locator(".chat-msg-assistant .chat-text", has_text="Answer 2").wait_for()
    assert [m["content"] for m in calls[1]["messages"]] == ["What do we know?", "Answer 1", "And next?"]

    page.reload()
    assert page.locator("#chat-dock").is_visible()  # stays open across pages in this tab
    assert page.locator(".chat-msg").count() == 4
    page.click("[data-chat-new]")
    assert page.locator(".chat-msg").count() == 0
    page.keyboard.press("Escape")
    assert not page.locator("#chat-dock").is_visible()


def test_chat_fits_a_phone(chat_app, page):
    url, _, _ = chat_app
    page.set_viewport_size(MOBILE)
    sign_in(page, url)
    page.click("#chat-launcher")
    box = page.locator("#chat-dock").bounding_box()
    assert box["x"] >= 0 and box["x"] + box["width"] <= MOBILE["width"]
    assert page.evaluate("document.documentElement.scrollWidth") <= MOBILE["width"]
