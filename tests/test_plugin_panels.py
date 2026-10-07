"""
Plugin panels (core/plugin_panels.py): cards a plugin adds to the dashboard
and prospect pages. Also the UI kit page (plugins/pages/ui_kit.py).

Run: TEST_DATABASE_URL=postgresql://localhost/agency_os_test python -m pytest tests/test_plugin_panels.py
"""

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from jinja2 import ChoiceLoader, DictLoader, Environment

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import web.app as webapp  # noqa: E402
from core import panels, plugin_pages, plugin_panels  # noqa: E402
from tests.test_access import client_for, db, make_user  # noqa: E402,F401
from tests.test_recruiting import leads  # noqa: E402,F401

TEMPLATES = {
    "plugin-panel/grants.html": "<p class='grants'>{{ count }} grants for {{ name }}</p>",
    "plugin-panel/team.html": "<p class='team'>Team of {{ size }} · {{ user.name }}</p>",
    "plugin-panel/broken.html": "{{ missing.attr.boom }}",
}


class Grants:
    key = "grants"
    title = "Open grants"
    slot = "prospect"
    permission = "prospects.view"
    template = "grants.html"
    width = "two-thirds"

    def context(self, panel):
        return {"count": 3, "name": panel.prospect.name} if panel.prospect else None


class Team:
    key = "team"
    title = "Team size"
    slot = "dashboard"
    permission = "dashboard.view"
    template = "team.html"

    def context(self, panel):
        return {"size": len(panel.campaigns)}


class OwnersOnly(Team):
    key = "owners-only"
    permission = "@owner"


class Typo(Team):
    key = "typo"
    permission = "dashboard.veiw"  # unknown permission: hidden from everyone


class Empty(Grants):
    key = "empty"

    def context(self, panel):
        return None


class Raises(Grants):
    key = "raises"

    def context(self, panel):
        raise RuntimeError("boom")


class BadTemplate(Grants):
    key = "bad-template"
    template = "broken.html"


class NoSlot(Grants):
    key = "no-slot"
    slot = "sidebar"


class WideWidth(Grants):
    key = "wide"
    width = "huge"


ALL = (Grants, Team, OwnersOnly, Typo, Empty, Raises, BadTemplate, NoSlot, WideWidth)


@pytest.fixture
def plugin_set(monkeypatch):
    class FakeRegistry:
        def __init__(self):
            self.panels = {}

        def discover(self, base_dir, categories=None):
            if categories == ("panels",):
                self.panels = {p.key: p() for p in ALL}

    monkeypatch.setattr(plugin_panels, "PluginRegistry", FakeRegistry)
    monkeypatch.setattr(plugin_panels, "_panels", None)
    yield
    plugin_panels._panels = None


@pytest.fixture
def app_templates(monkeypatch, plugin_set):
    env = webapp.templates.env
    monkeypatch.setattr(env, "loader", ChoiceLoader([env.loader, DictLoader(TEMPLATES)]))
    env.cache.clear() if env.cache is not None else None


def user(*allowed, owner=False):
    return SimpleNamespace(name="Rae", is_owner=owner,
                           allows=lambda rule: owner or rule in allowed)


def test_bad_panels_are_skipped_at_startup(plugin_set, capsys):
    assert list(plugin_panels.panels()) == ["grants", "team", "owners-only", "typo", "empty", "raises", "bad-template"]
    assert "panels/no-slot" in capsys.readouterr().out
    assert [p.key for p in plugin_panels.for_slot("dashboard")] == ["team", "owners-only", "typo"]


def test_render_shows_what_the_user_may_see_and_drops_broken_panels(plugin_set, capsys):
    env = Environment(loader=DictLoader(TEMPLATES), autoescape=True)
    prospect = SimpleNamespace(name="Civic <Org>")
    ctx = plugin_panels.PanelContext(user=user("prospects.view"), db=None, prospect=prospect)
    out = plugin_panels.render(env, "prospect", ctx)
    assert [(p["id"], p["width"]) for p in out] == [("plugin-grants", "two-thirds")]
    assert "3 grants for Civic &lt;Org&gt;" in out[0]["html"]  # escaped like any template
    logged = capsys.readouterr().out
    assert "panels/raises: RuntimeError: boom" in logged and "panels/bad-template" in logged

    assert plugin_panels.render(env, "prospect", plugin_panels.PanelContext(user=user(), db=None, prospect=prospect)) == []
    dash = plugin_panels.render(env, "dashboard", plugin_panels.PanelContext(user=user(owner=True), db=None, campaigns=[1, 2]))
    assert [p["key"] for p in dash] == ["team", "owners-only"]  # the typo'd permission hides it even from owners
    assert "Team of 2 · Rae" in dash[0]["html"]


def test_prospect_board_takes_plugin_panels_and_forgets_removed_ones(db, plugin_set):
    assert panels.page_panels("prospect")["plugin-grants"] == panels.Panel("Open grants", "two-thirds")
    layout = {"panels": [{"id": "plugin-grants", "width": "full"}, {"id": "org"}], "hidden": ["plugin-empty"]}
    make_user(db, "rep@x.com", "Sales Rep")
    rep_id = db.get_user_by_email("rep@x.com")["id"]
    panels.save(db, rep_id, "prospect", layout)
    board = panels.Board("prospect", panels.load(db, rep_id, "prospect"))
    assert board.order["plugin-grants"] == 0 and 'data-width="full"' in board.attrs("plugin-grants")

    plugin_panels._panels = {"empty": Empty()}  # the grants plugin was removed
    saved = panels.load(db, rep_id, "prospect")
    assert saved is not None and saved["panels"] == [{"id": "org", "width": "third", "color": None}]
    assert saved["hidden"] == ["plugin-empty"]
    with pytest.raises(panels.LayoutError, match="plugin-grants"):
        panels.validate("prospect", layout)  # a new save must name panels that exist


def test_panels_show_on_the_prospect_page_and_dashboard(db, leads, app_templates):
    make_user(db, "rep@x.com", "Sales Rep")
    make_user(db, "editor@x.com", "Template Editor")  # no prospects.view
    rep = client_for("rep@x.com")

    page = rep.get(f"/prospects/{leads['dentist']}").text
    assert 'data-panel="plugin-grants"' in page and "3 grants for Bright Smile Dental" in page
    assert "plugin-raises" not in page and "plugin-empty" not in page
    assert rep.get(f"/prospects/{leads['attorney']}").status_code == 404  # hidden campaign: no page, no panel

    dashboard = rep.get("/").text
    assert 'data-plugin-panel="team"' in dashboard and "Team of" in dashboard
    assert "owners-only" not in dashboard
    assert "Team of" in client_for("editor@x.com").get("/").text


def test_ui_kit_opens_for_anyone_but_stays_out_of_the_nav(db):
    plugin_pages._pages = None
    make_user(db, "caller@x.com", "Caller")
    caller = client_for("caller@x.com")
    kit = caller.get("/p/ui-kit")
    assert kit.status_code == 200
    assert 'class="stage-badge stage-cold"' in kit.text and "&lt;div class=&#34;page-header&#34;&gt;" in kit.text
    assert 'href="/p/ui-kit"' not in caller.get("/").text
