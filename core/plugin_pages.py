"""
Plugin pages: a plugin can add its own page to the web app without touching core.

Drop plugins/pages/<name>.py with a class like:

    class HelloPage:
        key = "hello"                    # served at /p/hello
        title = "Hello"                  # nav label
        permission = "dashboard.view"    # who may open it (see below)
        template = "hello.html"          # in plugins/pages/templates/

        def context(self, page: PageContext) -> dict:
            return {"greeting": f"Hi {page.user.name}"}

        # Optional: handle the page's own form (POST /p/hello). Return the
        # message to show; raise ValueError to show an error instead.
        def post(self, page: PageContext, form: dict) -> str:
            return "Saved."

Templates live in plugins/pages/templates/ and are loaded as "plugin/<name>",
so they can {% extends "base.html" %} and use the core filters, but can never
replace a core template. Files in plugins/pages/static/ are served at
/plugin-static/ (public, like /static: no secrets there).

Access stays in core. `permission` must be a permission from access.CATALOG,
"@owner" or "@super_admin"; anything else (a typo, "@public", "@user") denies
everyone. Posting needs `post_permission` if the class sets one, else
`permission`. Plugins can't add permissions: that's a change to core/access.py.

Prospect data: a page must respect campaign visibility. PageContext carries
the campaigns this user sees and the names of those they don't
(`hidden_campaigns`, which the Database list queries take).

Pages are discovered once per process, like the other plugins (restart to add one).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from core import access
from core.registry import PluginRegistry

PAGES_DIR = Path(__file__).resolve().parent.parent / "plugins" / "pages"
TEMPLATES_DIR = PAGES_DIR / "templates"
STATIC_DIR = PAGES_DIR / "static"
TEMPLATE_PREFIX = "plugin"
KEY_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_RULES = (access.OWNER, access.SUPER_ADMIN)

_pages: dict[str, Any] | None = None


@dataclass
class PageContext:
    """What a plugin page gets to work with for one request."""
    user: access.CurrentUser
    db: Any
    query: dict[str, str] = field(default_factory=dict)
    campaigns: list = field(default_factory=list)          # campaigns this user sees
    hidden_campaigns: list[str] = field(default_factory=list)  # names they don't


def _valid(page) -> bool:
    return (isinstance(getattr(page, "key", None), str) and bool(KEY_RE.match(page.key))
            and isinstance(getattr(page, "title", None), str) and bool(page.title)
            and isinstance(getattr(page, "template", None), str) and bool(page.template)
            and callable(getattr(page, "context", None)))


def pages() -> dict[str, Any]:
    """Every valid plugin page by key, in discovery order."""
    global _pages
    if _pages is None:
        registry = PluginRegistry()
        registry.discover(str(PAGES_DIR.parent), categories=("pages",))
        _pages = {}
        for key, page in registry.pages.items():
            if _valid(page):
                _pages[key] = page
            else:
                print(f"  ! pages/{key}: needs a lowercase key, a title, a template and context()")
    return _pages


def get(key: str):
    return pages().get(key)


def _permits(user: access.CurrentUser, rule) -> bool:
    if not isinstance(rule, str) or not (rule in access.CATALOG or rule in _RULES):
        return False  # unknown or too-open rules deny everyone
    return user.allows(rule)


def can_view(user: access.CurrentUser, page) -> bool:
    return _permits(user, getattr(page, "permission", None))


def can_post(user: access.CurrentUser, page) -> bool:
    if not callable(getattr(page, "post", None)):
        return False
    return can_view(user, page) and _permits(
        user, getattr(page, "post_permission", None) or getattr(page, "permission", None))


def visible_to(user) -> list:
    """The pages this user may open, for the nav."""
    return [p for p in pages().values() if user and can_view(user, p)]


def template_name(page) -> str:
    return f"{TEMPLATE_PREFIX}/{page.template}"
