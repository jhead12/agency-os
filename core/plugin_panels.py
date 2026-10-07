"""
Plugin panels: a plugin adds a card to a core page without touching core.

Drop plugins/panels/<name>.py with a class like:

    class OpenGrantsPanel:
        key = "open-grants"              # unique among panels
        title = "Open grants"            # the card's label (and in the layout editor)
        slot = "prospect"                # "prospect" (each prospect's page) or "dashboard"
        permission = "prospects.view"    # who sees it (the same rules as plugin pages)
        template = "open_grants.html"    # in plugins/panels/templates/
        width = "third"                  # optional: "third", "two-thirds" or "full"
        shown = True                     # optional: False puts it in the layout library, off until added

        def context(self, panel: PanelContext) -> dict | None:
            return {"grants": [...]}     # None (or {}) hides the card: nothing to show

The card goes on the page for everyone `permission` allows. On the prospect
page it joins the customizable board (core/panels.py) as "plugin-<key>", so
people move, resize, color and hide it like the built-in panels.

Panels only read. A panel that needs a form links to a plugin page
(plugins/pages/) and handles the post there. A panel whose context() or
template fails is left off the page and logged; it never breaks the page.

On the prospect page, `panel.prospect` is a prospect this user is allowed to
see (the app checks before the page loads). Anything else a panel queries must
still leave out `panel.hidden_campaigns`, as plugin pages do.

Panels are discovered once per process (restart to add one); templates
reload on refresh.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from markupsafe import Markup

from core import access, plugin_pages
from core.registry import PluginRegistry

PANELS_DIR = Path(__file__).resolve().parent.parent / "plugins" / "panels"
TEMPLATES_DIR = PANELS_DIR / "templates"
TEMPLATE_PREFIX = "plugin-panel"
SLOTS = ("prospect", "dashboard")
WIDTHS = ("third", "two-thirds", "full")  # the same as core.panels.WIDTHS
ID_PREFIX = "plugin-"  # a panel's id on a page board: plugin-<key>
KEY_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,47}$")

_panels: dict[str, Any] | None = None


@dataclass
class PanelContext:
    """What a plugin panel gets for one page view."""
    user: access.CurrentUser
    db: Any
    campaigns: list = field(default_factory=list)              # campaigns this user sees
    hidden_campaigns: list[str] = field(default_factory=list)  # names they don't
    prospect: Any = None                                       # the prospect, on the prospect page


def _valid(panel) -> bool:
    return (isinstance(getattr(panel, "key", None), str) and bool(KEY_RE.match(panel.key))
            and isinstance(getattr(panel, "title", None), str) and bool(panel.title)
            and getattr(panel, "slot", None) in SLOTS
            and isinstance(getattr(panel, "template", None), str) and bool(panel.template)
            and getattr(panel, "width", "third") in WIDTHS
            and callable(getattr(panel, "context", None)))


def panels() -> dict[str, Any]:
    """Every valid plugin panel by key, in discovery order."""
    global _panels
    if _panels is None:
        registry = PluginRegistry()
        registry.discover(str(PANELS_DIR.parent), categories=("panels",))
        _panels = {}
        for key, panel in registry.panels.items():
            if _valid(panel):
                _panels[key] = panel
            else:
                print(f"  ! panels/{key}: needs a lowercase key, a title, a slot ({' or '.join(SLOTS)}), "
                      f"a template and context(); width is {', '.join(WIDTHS)}")
    return _panels


def for_slot(slot: str) -> list:
    return [p for p in panels().values() if p.slot == slot]


def panel_id(panel) -> str:
    return f"{ID_PREFIX}{panel.key}"


def width(panel) -> str:
    return getattr(panel, "width", "third")


def can_view(user, panel) -> bool:
    return bool(user) and plugin_pages.can_view(user, panel)


def render(env, slot: str, ctx: PanelContext) -> list[dict]:
    """The panels this user sees in a slot, rendered: [{id, key, title, width, html}].

    Runs each panel's context() and template; one that fails or has nothing
    to show is left out.
    """
    out = []
    for panel in for_slot(slot):
        if not can_view(ctx.user, panel):
            continue
        try:
            values = panel.context(ctx)
            if not values:
                continue
            html = env.get_template(f"{TEMPLATE_PREFIX}/{panel.template}").render(
                {**values, "panel": panel, "user": ctx.user})
        except Exception as exc:  # a broken plugin never takes the page down
            print(f"  ! panels/{panel.key}: {type(exc).__name__}: {exc}")
            continue
        out.append({"id": panel_id(panel), "key": panel.key, "title": panel.title,
                    "width": width(panel), "html": Markup(html)})
    return out
