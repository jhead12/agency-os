"""
Customizable page panels: each user arranges a page's panels their own way.

A page (only "prospect", the prospect detail page, so far) is a board of
panels. A user can reorder them, resize them (a third, two thirds or the full
width), color them, hide them, add extra data panels from the library, and
color the page background. The arrangement is saved per user in user_layouts;
with none saved the page uses its defaults.

Panels the user may not see (no permission, no data) are never rendered, so a
saved layout can name them safely: they're skipped.

Layout, as saved and as posted by the editor (web/static/panel_editor.js):
    {"background": "#1e293b" | null,
     "panels": [{"id": "org", "width": "third", "color": "#123456" | null}, ...],
     "hidden": ["agent", ...]}
"panels" is the visible ones in order. A panel in neither list (one added to
the page after the user saved) shows with its defaults, at the end.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

from markupsafe import Markup, escape

WIDTHS = ("third", "two-thirds", "full")
COLOR = re.compile(r"^#[0-9a-f]{6}$")


class LayoutError(ValueError):
    """A layout that can't be saved; the message says what to fix."""


@dataclass(frozen=True)
class Panel:
    label: str
    width: str = "third"
    shown: bool = True       # in the default layout
    about: str = ""          # shown in the editor's library


PAGES: dict[str, dict[str, Panel]] = {
    "prospect": {
        "org": Panel("Organization info"),
        "pipeline": Panel("Pipeline"),
        "contact": Panel("Contact info"),
        "agent": Panel("Ask an agent", "full"),
        "credit": Panel("Who built this lead", "full"),
        "guarantee": Panel("Package guarantee", "full"),
        "portal": Panel("Demo page", "full"),
        "emails": Panel("Email history", "full"),
        "log-call": Panel("Log a call", "full"),
        "calls": Panel("Call history", "full"),
        # Library panels: off until a user adds them.
        "last-contact": Panel("Last contact", shown=False,
                              about="When they were last reached, how, and how it went"),
        "follow-up": Panel("Follow-up", shown=False,
                           about="The next follow-up date, whether it's overdue, and the agreed next step"),
        "glance": Panel("At a glance", shown=False,
                        about="Stage, contact and phone, revenue and focus area in one small card"),
    },
}


# The prospect page's background follows the pipeline stage unless the user
# picked their own background. Dark tints, so every panel stays readable.
STAGE_BACKGROUNDS = {
    "cold": "#0a0a0a",            # black
    "contacted": "#0b1730",       # blue
    "engaged": "#0b2614",         # green
    "demo_scheduled": "#1f1236",  # purple
    "proposal_sent": "#2d1b08",   # orange
    "closed_won": "#2a2306",      # gold
    "closed_lost": "#2a0b0b",     # red
    "nurture": "#082429",         # teal
}


def tone(color: str) -> str:
    """"light" or "dark": which text color reads on this background."""
    r, g, b = (int(color[i:i + 2], 16) / 255 for i in (1, 3, 5))
    lin = [c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in (r, g, b)]
    return "light" if 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2] > 0.18 else "dark"


def _color(value, where: str):
    if value in (None, ""):
        return None
    value = str(value).strip().lower()
    if not COLOR.match(value):
        raise LayoutError(f"{where}: colors look like #1e293b (got {value[:20]!r})")
    return value


def _placed(panels: dict, item, seen: set) -> dict:
    """One entry of a layout's "panels", checked."""
    pid = item.get("id") if isinstance(item, dict) else None
    if pid not in panels:
        raise LayoutError(f"Unknown panel {pid!r}; panels are: {', '.join(panels)}")
    if pid in seen:
        raise LayoutError(f"Panel {pid!r} is listed twice")
    width = item.get("width") or panels[pid].width
    if width not in WIDTHS:
        raise LayoutError(f"Panel {pid!r}: width is one of {', '.join(WIDTHS)}")
    seen.add(pid)
    return {"id": pid, "width": width, "color": _color(item.get("color"), f"Panel {pid!r}")}


def validate(page: str, layout) -> dict:
    """The layout, cleaned up, or LayoutError."""
    panels = PAGES.get(page)
    if panels is None:
        raise LayoutError(f"No customizable page called {page!r}")
    if not isinstance(layout, dict):
        raise LayoutError("A layout is an object with background, panels and hidden")
    unknown = set(layout) - {"background", "panels", "hidden"}
    if unknown:
        raise LayoutError(f"Unknown field {min(unknown)!r}")
    seen, shown = set(), []
    for item in layout.get("panels") or []:
        shown.append(_placed(panels, item, seen))
    hidden = []
    for pid in layout.get("hidden") or []:
        if pid not in panels:
            raise LayoutError(f"Unknown panel {pid!r}; panels are: {', '.join(panels)}")
        if pid in seen:
            raise LayoutError(f"Panel {pid!r} can't be both shown and hidden")
        seen.add(pid)
        hidden.append(pid)
    return {"background": _color(layout.get("background"), "Background"), "panels": shown, "hidden": hidden}


def load(db, user_id: int, page: str) -> dict | None:
    row = db.conn.execute("SELECT layout FROM user_layouts WHERE user_id = ? AND page = ?",
                          (user_id, page)).fetchone()
    if not row:
        return None
    try:
        return validate(page, json.loads(row["layout"]))
    except ValueError:  # a LayoutError too: panels renamed since: fall back to the defaults
        return None


def save(db, user_id: int, page: str, layout) -> None:
    """Save the user's layout for a page; None goes back to the defaults."""
    if layout is None:
        with db.transaction() as c:
            c.execute("DELETE FROM user_layouts WHERE user_id = ? AND page = ?", (user_id, page))
        return
    layout = validate(page, layout)
    with db.transaction() as c:
        c.execute("""INSERT INTO user_layouts (user_id, page, layout) VALUES (?, ?, ?)
                     ON CONFLICT (user_id, page) DO UPDATE
                     SET layout = EXCLUDED.layout, updated_at = CURRENT_TIMESTAMP""",
                  (user_id, page, json.dumps(layout)))


class Board:
    """What a template needs to draw a page's panels in the user's layout."""

    def __init__(self, page: str, layout: dict | None):
        self.page = page
        self.panels = PAGES[page]
        self.custom = layout is not None
        layout = layout or {}
        self.background = layout.get("background")
        hidden = set(layout.get("hidden") or [])
        placed = {p["id"]: p for p in layout.get("panels") or []}
        order = [p["id"] for p in layout.get("panels") or []]
        order += [pid for pid in self.panels if pid not in placed and pid not in hidden]
        order += [pid for pid in self.panels if pid in hidden]
        self.order = {pid: i for i, pid in enumerate(order)}
        self.width = {pid: placed.get(pid, {}).get("width") or p.width for pid, p in self.panels.items()}
        self.color = {pid: placed.get(pid, {}).get("color") for pid in self.panels}
        self.hidden = {pid for pid, p in self.panels.items() if pid in hidden or (pid not in placed and not p.shown)}

    @property
    def background_tone(self) -> str:
        return tone(self.background) if self.background else ""

    def attrs(self, pid: str) -> Markup:
        """The attributes for a panel's element: <div class="detail-card" {{ board.attrs('org') }}>."""
        panel = self.panels[pid]
        style = f"order: {self.order[pid]};"
        out = (f'data-panel="{escape(pid)}" data-panel-label="{escape(panel.label)}" '
               f'data-panel-about="{escape(panel.about)}" data-width="{self.width[pid]}"')
        color = self.color[pid]
        if color:
            style += f" --panel-color: {color};"
            out += f' data-panel-color="{color}" data-panel-tone="{tone(color)}"'
        if pid in self.hidden:
            out += " hidden"
        return Markup(f'{out} style="{style}"')
