"""
`agency-os new-plugin <name>`: a working plugin, every part wired together,
from the starter templates in plugins/_starter/.

    plugins/pages/<name>.py                 a page at /p/<key>: search, the job's findings, save as a list
    plugins/pages/templates/<name>.html     its HTML
    plugins/pages/static/<name>.css         its styles
    plugins/panels/<name>.py                a card on each prospect's page (+ templates/<name>.html)
    plugins/prospect_sources/<name>.py      prospects from a JSON feed (off until its URL is set)
    plugins/jobs/<name>.py                  a daily AI job: the agent picks who to work next
    plugins/agents/<key>.md                 the agent (persona) with its own task
    tests/test_plugin_<name>.py             checks they load and work together

Nothing outside those files changes, and an existing file is never
overwritten. The templates use string.Template placeholders: ${module}
(grant_finder), ${key} (grant-finder), ${Class} (GrantFinder), ${title}
(Grant finder) and ${ENV} (GRANT_FINDER).
"""

from __future__ import annotations

import re
from pathlib import Path
from string import Template

from core.jobs import BUILT_IN

PROJECT_ROOT = Path(__file__).resolve().parent.parent
STARTER_DIR = PROJECT_ROOT / "plugins" / "_starter"
NAME_RE = re.compile(r"^[a-z][a-z0-9_]{2,23}$")
TITLE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 -]{0,39}$")

# starter template -> where it goes
FILES = {
    "page.py.tmpl": "plugins/pages/${module}.py",
    "page.html.tmpl": "plugins/pages/templates/${module}.html",
    "page.css.tmpl": "plugins/pages/static/${module}.css",
    "panel.py.tmpl": "plugins/panels/${module}.py",
    "panel.html.tmpl": "plugins/panels/templates/${module}.html",
    "source.py.tmpl": "plugins/prospect_sources/${module}.py",
    "job.py.tmpl": "plugins/jobs/${module}.py",
    "agent.md.tmpl": "plugins/agents/${key}.md",
    "test.py.tmpl": "tests/test_plugin_${module}.py",
}


class ScaffoldError(ValueError):
    """The plugin wasn't created; the message says what to change."""


def names(name: str, title: str = "") -> dict[str, str]:
    """The placeholder values for a plugin name like grant-finder or grant_finder."""
    module = (name or "").strip().lower().replace("-", "_")
    if not NAME_RE.match(module):
        raise ScaffoldError("Name the plugin with 3-24 lowercase letters, digits, - or _, "
                            "starting with a letter (e.g. grant-finder)")
    key = module.replace("_", "-")
    if key in BUILT_IN:
        raise ScaffoldError(f"{key} is a built-in job; pick another name")
    title = (title or module.replace("_", " ").capitalize()).strip()
    if not TITLE_RE.match(title):
        raise ScaffoldError("Keep the title to 40 letters, digits, spaces or hyphens")
    return {"module": module, "key": key, "Class": "".join(w.capitalize() for w in module.split("_")),
            "title": title, "ENV": module.upper()}


def plan(name: str, title: str = "", root: Path = PROJECT_ROOT) -> list[tuple[Path, Path]]:
    """(template, target) for each file, or ScaffoldError if any target exists."""
    values = names(name, title)
    pairs = [(STARTER_DIR / src, root / Template(dest).substitute(values)) for src, dest in FILES.items()]
    taken = [str(target.relative_to(root)) for _, target in pairs if target.exists()]
    if (root / "agents" / f"{values['key']}.md").exists():
        taken.append(f"agents/{values['key']}.md")
    if taken:
        raise ScaffoldError(f"Already exists: {', '.join(taken)}. Pick another name.")
    return pairs


def create(name: str, title: str = "", root: Path = PROJECT_ROOT) -> list[Path]:
    """Write the plugin's files. Returns the paths written."""
    values = names(name, title)
    written = []
    for template, target in plan(name, title, root):
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(Template(template.read_text(encoding="utf-8")).substitute(values), encoding="utf-8")
        written.append(target)
    return written
