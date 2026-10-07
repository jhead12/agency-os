"""UI kit: the app's styles and components, live, each with HTML to copy.

A reference for building plugin pages and panels: /p/ui-kit. It shows only
sample data and isn't in the nav (`nav = False`); link to it from docs.
"""

from core.panels import STAGE_BACKGROUNDS

COLORS = ("bg", "surface", "surface-hover", "border", "text", "text-muted", "accent", "accent-hover",
          "green", "yellow", "red", "purple", "orange", "cyan")
STATUSES = ("sent", "opened", "replied", "bounced", "skipped")


class UiKitPage:
    key = "ui-kit"
    title = "UI kit"
    permission = "dashboard.view"
    template = "ui_kit.html"
    nav = False  # a developer reference, not a page people work in

    def context(self, page):
        return {"colors": COLORS, "stages": list(STAGE_BACKGROUNDS), "statuses": STATUSES}
