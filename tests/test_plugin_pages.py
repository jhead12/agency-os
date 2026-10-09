"""
Tests for plugin pages (core/plugin_pages.py): plugins/pages/ adds pages at
/p/<key> whose access is still decided by core.

Run: python -m pytest tests/
"""

import sys
from pathlib import Path

import pytest
from jinja2 import FileSystemLoader

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import web.app as webapp  # noqa: E402
from core import access, plugin_pages  # noqa: E402
from core.registry import PluginRegistry  # noqa: E402
from tests.test_access import client_for, db, make_user  # noqa: E402,F401


class StatsPage:
    key = "stats"
    title = "Team stats"
    permission = "calls.view"
    template = "stats.html"

    def context(self, page):
        return {"greeting": f"Hi {page.user.name}", "seen": len(page.campaigns)}


class FormPage:
    key = "form"
    title = "Form"
    permission = "dashboard.view"
    post_permission = "templates.edit"
    template = "stats.html"
    posts = []

    def context(self, page):
        return {"greeting": "form"}

    def post(self, page, form):
        if form.get("name") == "bad":
            raise ValueError("Name can't be bad.")
        if form.get("name") == "file":
            return plugin_pages.Download('../evil"name.csv', "a,b\n")
        FormPage.posts.append((page.user.email, form))
        return f"Saved {form['name']}."


class OpenPage(StatsPage):
    key = "open"
    title = "Open"
    permission = access.PUBLIC


class TypoPage(StatsPage):
    key = "typo"
    title = "Typo"
    permission = "calls.veiw"


@pytest.fixture
def pages(tmp_path, monkeypatch):
    (tmp_path / "stats.html").write_text(
        '{% extends "base.html" %}{% block content %}'
        '<h1>{{ plugin_page.title }}</h1><p>{{ greeting }} / {{ seen }}</p>'
        '{% if msg %}<div class="save-notice">{{ msg }}</div>{% endif %}'
        '{% if error %}<div class="form-error">{{ error }}</div>{% endif %}'
        '{% endblock %}')
    prefix_loader = webapp.templates.env.loader.loaders[1]
    monkeypatch.setitem(prefix_loader.mapping, plugin_pages.TEMPLATE_PREFIX, FileSystemLoader(str(tmp_path)))
    webapp.templates.env.cache.clear()
    FormPage.posts = []
    monkeypatch.setattr(plugin_pages, "_pages", {p.key: p() for p in (StatsPage, FormPage, OpenPage, TypoPage)})
    yield
    webapp.templates.env.cache.clear()


def test_page_renders_in_the_app_layout_for_users_with_its_permission(db, pages):
    make_user(db, "caller@x.com", "Caller")
    r = client_for("caller@x.com").get("/p/stats")
    assert r.status_code == 200
    assert "<h1>Team stats</h1>" in r.text and "Hi caller / " in r.text
    assert 'class="navbar"' in r.text and 'href="/p/stats"' in r.text


def test_page_is_forbidden_and_hidden_without_its_permission(db, pages):
    make_user(db, "editor@x.com", "Template Editor")
    client = client_for("editor@x.com")
    assert client.get("/p/stats").status_code == 403
    assert 'href="/p/stats"' not in client.get("/p/form").text


def test_guests_are_sent_to_login_and_unknown_pages_404(db, pages):
    assert client_for().get("/p/stats").headers["location"] == "/login?next=/p/stats"
    make_user(db, "owner@x.com", access.OWNER_ROLE)
    assert client_for("owner@x.com").get("/p/nope").status_code == 404


def test_public_or_misspelled_permission_denies_everyone(db, pages):
    make_user(db, "owner@x.com", access.OWNER_ROLE)
    client = client_for("owner@x.com")
    assert client.get("/p/open").status_code == 403
    assert client.get("/p/typo").status_code == 403
    assert client_for().get("/p/open").status_code == 303
    nav = client.get("/p/stats").text
    assert 'href="/p/open"' not in nav and 'href="/p/typo"' not in nav


def test_post_needs_the_post_permission_and_shows_its_message(db, pages):
    make_user(db, "viewer@x.com", "Viewer")
    make_user(db, "editor@x.com", "Template Editor")
    assert client_for("viewer@x.com").post("/p/form", data={"name": "x"}).status_code == 403

    client = client_for("editor@x.com")
    r = client.post("/p/form", data={"name": "Ada"})
    assert r.status_code == 303 and r.headers["location"] == "/p/form?msg=Saved%20Ada."
    assert FormPage.posts == [("editor@x.com", {"name": "Ada"})]
    assert "Saved Ada." in client.get(r.headers["location"]).text

    r = client.post("/p/form", data={"name": "bad"})
    assert r.headers["location"] == "/p/form?error=Name%20can%27t%20be%20bad."


def test_post_can_send_a_file_with_a_safe_name(db, pages):
    make_user(db, "editor@x.com", "Template Editor")
    r = client_for("editor@x.com").post("/p/form", data={"name": "file"})
    assert r.status_code == 200 and r.text == "a,b\n"
    assert r.headers["content-disposition"] == 'attachment; filename="evil_name.csv"'


def test_page_without_post_refuses_posts(db, pages):
    make_user(db, "caller@x.com", "Caller")
    assert client_for("caller@x.com").post("/p/stats", data={}).status_code == 403


def test_cross_site_post_is_refused(db, pages):
    make_user(db, "editor@x.com", "Template Editor")
    r = client_for("editor@x.com").post("/p/form", data={"name": "x"},
                                        headers={"origin": "https://evil.example"})
    assert r.status_code == 403 and FormPage.posts == []


def test_pages_are_not_in_the_other_plugin_categories():
    registry = PluginRegistry()
    registry.discover(str(Path(webapp.PROJECT_ROOT) / "plugins"))
    assert registry.pages == {} and "pages" not in registry.list_plugins()
