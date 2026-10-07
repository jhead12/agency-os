"""
Workflows: replayable recipes that the in-browser player (web/static/player.js)
acts out on the user's own screen. It moves a visible cursor, highlights and
fills fields, clicks, changes pages, runs console commands and explains each
step, so people can watch how agency-os is used (tutorials) or replay their
own routines (automations).

A workflow is data, never code:

    name: Find a prospect
    description: Search the list and open a prospect.
    requires: prospects.view          # tutorials: who sees it (catalog permission or @owner/@super_admin)
    steps:
      - goto: /prospects
        say: This is your prospect list.
      - fill: {target: "input[name=q]", value: food}
      - click: "#filter-form button[type=submit]"
      - highlight: "table.data-table"
        say: Matching prospects show here.
      - run: search-prospects --q food --limit 3
      - pause: Your turn. Open one of them.

Steps (one action each, plus an optional `say` caption):
goto <app path> · say <text> · highlight <selector> · fill {target, value} ·
click <selector> · wait <ms> | {for: <selector>} · run <console command> ·
pause <text>.

It's safe to import someone else's workflow: it can only do what the person
playing it could do by hand. Pages are limited to this app, a click that
submits a change and a console command that changes data both ask first, and
every change is audited as the person who confirmed it.

Built-in tutorials live in workflows/tutorials/*.yaml. Users' own workflows
are stored per user and can be exported to a JSON backup and imported back.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path

import yaml

from core import access
from core.access import CurrentUser

TUTORIALS_DIR = Path(__file__).resolve().parent.parent / "workflows" / "tutorials"
BACKUP_FORMAT = "agency-os-workflows"
BACKUP_VERSION = 1
MAX_STEPS = 200
MAX_DEFINITION = 100_000  # bytes of JSON
MAX_PER_USER = 200
ACTIONS = ("goto", "say", "highlight", "fill", "click", "wait", "run", "pause")


class WorkflowError(ValueError):
    """A workflow was rejected; the message says what to fix."""


def slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:80] or "workflow"


def _text(value, where: str, limit: int) -> str:
    if not isinstance(value, (str, int, float)) or isinstance(value, bool):
        raise WorkflowError(f"{where} must be text")
    value = str(value)
    if len(value) > limit:
        raise WorkflowError(f"{where} is too long (max {limit} characters)")
    return value


def _path(value, where: str) -> str:
    path = _text(value, where, 500).strip()
    # Only pages of this app: a path, never another site (//host, scheme:...).
    if not path.startswith("/") or path.startswith("//") or "\\" in path or re.match(r"/+[a-z]+:", path, re.I):
        raise WorkflowError(f"{where} must be a page of this app, like /prospects")
    return path


def _selector(value, where: str) -> str:
    selector = _text(value, where, 300).strip()
    if not selector:
        raise WorkflowError(f"{where} needs a CSS selector, like \"input[name=q]\"")
    return selector


def _step(raw, n: int) -> dict:
    where = f"step {n}"
    if isinstance(raw, str):  # a bare string is a caption
        raw = {"say": raw}
    if not isinstance(raw, dict):
        raise WorkflowError(f"{where} must be a mapping like {{goto: /prospects}}")
    unknown = set(raw) - set(ACTIONS)
    if unknown:
        raise WorkflowError(f"{where}: unknown action {sorted(unknown)[0]!r}. Use one of: {', '.join(ACTIONS)}")
    actions = [a for a in ACTIONS if a in raw and a != "say"]
    if len(actions) > 1:
        raise WorkflowError(f"{where} has {' and '.join(actions)}; put each action in its own step")
    step: dict = {}
    if "say" in raw:
        step["say"] = _text(raw["say"], f"{where} say", 1000)
    if not actions:
        if "say" not in step:
            raise WorkflowError(f"{where} is empty")
        return step
    action = actions[0]
    value = raw[action]
    if action == "goto":
        step["goto"] = _path(value, f"{where} goto")
    elif action in ("highlight", "click"):
        step[action] = _selector(value, f"{where} {action}")
    elif action == "fill":
        if not isinstance(value, dict) or set(value) - {"target", "value"} or "target" not in value:
            raise WorkflowError(f"{where} fill must look like {{target: \"input[name=q]\", value: food}}")
        step["fill"] = {"target": _selector(value["target"], f"{where} fill target"),
                        "value": _text(value.get("value", ""), f"{where} fill value", 2000)}
    elif action == "wait":
        if isinstance(value, int) and not isinstance(value, bool):
            if not 0 <= value <= 60_000:
                raise WorkflowError(f"{where} wait must be 0-60000 milliseconds")
            step["wait"] = value
        elif isinstance(value, dict) and set(value) == {"for"}:
            step["wait"] = {"for": _selector(value["for"], f"{where} wait for")}
        else:
            raise WorkflowError(f"{where} wait must be milliseconds (500) or {{for: <selector>}}")
    elif action == "run":
        step["run"] = _text(value, f"{where} run", 4000).strip()
        if not step["run"]:
            raise WorkflowError(f"{where} run needs a console command, like help")
    elif action == "pause":
        step["pause"] = _text(value, f"{where} pause", 1000)
    return step


def validate(raw) -> dict:
    """A clean workflow definition from parsed YAML/JSON, or WorkflowError."""
    if not isinstance(raw, dict):
        raise WorkflowError("A workflow must be a mapping with name and steps")
    unknown = set(raw) - {"name", "description", "requires", "steps"}
    if unknown:
        raise WorkflowError(f"Unknown field {sorted(unknown)[0]!r}; a workflow has name, description, steps "
                            "(and requires, for tutorials)")
    name = _text(raw.get("name", ""), "name", 120).strip()
    if not name:
        raise WorkflowError("A workflow needs a name")
    steps = raw.get("steps")
    if not isinstance(steps, list) or not steps:
        raise WorkflowError("A workflow needs a list of steps")
    if len(steps) > MAX_STEPS:
        raise WorkflowError(f"A workflow can have at most {MAX_STEPS} steps")
    clean = {"name": name, "description": _text(raw.get("description", ""), "description", 500).strip(),
             "steps": [_step(s, i + 1) for i, s in enumerate(steps)]}
    if raw.get("requires"):
        requires = _text(raw["requires"], "requires", 64)
        if requires not in access.CATALOG and requires not in (access.OWNER, access.SUPER_ADMIN):
            raise WorkflowError(f"requires must be a permission (e.g. prospects.view), {access.OWNER} "
                                f"or {access.SUPER_ADMIN}")
        clean["requires"] = requires
    if len(json.dumps(clean)) > MAX_DEFINITION:
        raise WorkflowError("That workflow is too large")
    return clean


def parse(text: str) -> dict:
    """Validate a workflow written as YAML or JSON (JSON is YAML)."""
    try:
        raw = yaml.safe_load(text or "")
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        where = f" (line {mark.line + 1})" if mark else ""
        raise WorkflowError(f"Couldn't read that as YAML or JSON{where}") from exc
    return validate(raw)


# ── Tutorials (built in) ───────────────────────────────────────────────


def tutorials(user: CurrentUser) -> list[dict]:
    """The built-in tutorials this user may play, in file order (01-..., 02-...)."""
    found = []
    for path in sorted(TUTORIALS_DIR.glob("*.yaml")):
        wf = parse(path.read_text())
        if user.allows(wf.get("requires") or access.ANY_USER):
            found.append({**wf, "slug": path.stem.split("-", 1)[-1] if path.stem[:2].isdigit() else path.stem,
                          "source": "tutorial"})
    return found


def tutorial(user: CurrentUser, slug: str) -> dict | None:
    return next((t for t in tutorials(user) if t["slug"] == slug), None)


# ── A user's own workflows ─────────────────────────────────────────────


def mine(db, user: CurrentUser) -> list[dict]:
    rows = db.conn.execute(
        "SELECT id, slug, definition, updated_at FROM workflows WHERE owner_id = ? ORDER BY name",
        (user.id,)).fetchall()
    return [{**json.loads(r["definition"]), "id": r["id"], "slug": r["slug"], "source": "mine",
             "updated_at": r["updated_at"]} for r in rows]


def get_mine(db, user: CurrentUser, slug: str) -> dict | None:
    return next((w for w in mine(db, user) if w["slug"] == slug), None)


def save(db, user: CurrentUser, definition: dict, *, replace: bool = True) -> str:
    """Save (or, with replace, overwrite by name) one of the user's workflows. Returns its slug."""
    definition = validate(definition)
    definition.pop("requires", None)  # only tutorials restrict who sees them
    slug = slugify(definition["name"])
    with db.transaction() as c:
        exists = c.execute("SELECT id FROM workflows WHERE owner_id = ? AND slug = ?", (user.id, slug)).fetchone()
        if exists and not replace:
            raise WorkflowError(f"You already have a workflow named {definition['name']!r}")
        if not exists:
            count = c.execute("SELECT COUNT(*) AS n FROM workflows WHERE owner_id = ?", (user.id,)).fetchone()["n"]
            if count >= MAX_PER_USER:
                raise WorkflowError(f"You can keep up to {MAX_PER_USER} workflows; delete one first")
        c.execute(
            """INSERT INTO workflows (owner_id, slug, name, definition) VALUES (?, ?, ?, ?)
               ON CONFLICT (owner_id, slug) DO UPDATE
               SET name = EXCLUDED.name, definition = EXCLUDED.definition, updated_at = CURRENT_TIMESTAMP""",
            (user.id, slug, definition["name"], json.dumps(definition)))
        db._audit(c, user, "workflow.save", "workflow", slug, {"name": definition["name"],
                                                                "steps": len(definition["steps"])})
    return slug


def delete(db, user: CurrentUser, slug: str) -> bool:
    with db.transaction() as c:
        gone = c.execute("DELETE FROM workflows WHERE owner_id = ? AND slug = ? RETURNING id",
                         (user.id, slug)).fetchone()
        if gone:
            db._audit(c, user, "workflow.delete", "workflow", slug, {})
    return gone is not None


# ── Backup ─────────────────────────────────────────────────────────────


def export(db, user: CurrentUser) -> dict:
    """A backup of all the user's workflows (import it here or on another agency-os)."""
    keep = ("name", "description", "steps")
    return {"format": BACKUP_FORMAT, "version": BACKUP_VERSION,
            "exported_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "exported_by": user.email,
            "workflows": [{k: w[k] for k in keep if k in w} for w in mine(db, user)]}


def import_text(db, user: CurrentUser, text: str) -> list[str]:
    """Import a backup file, or a single workflow (YAML/JSON). Same-named workflows are replaced.

    Everything is checked before anything is saved, so a bad file changes nothing.
    """
    try:
        raw = yaml.safe_load(text or "")
    except yaml.YAMLError as exc:
        raise WorkflowError("Couldn't read that file as a workflow backup, YAML or JSON") from exc
    if isinstance(raw, dict) and raw.get("format") == BACKUP_FORMAT:
        if not isinstance(raw.get("workflows"), list):
            raise WorkflowError("That backup has no workflows list")
        items = raw["workflows"]
    else:
        items = [raw]
    if not items:
        raise WorkflowError("That backup is empty")
    clean = []
    for i, item in enumerate(items, 1):
        try:
            clean.append(validate(item))
        except WorkflowError as exc:
            label = item.get("name") if isinstance(item, dict) and item.get("name") else f"workflow {i}"
            raise WorkflowError(f"{label}: {exc}") from exc
    have = {w["slug"] for w in mine(db, user)}
    new = {slugify(wf["name"]) for wf in clean} - have
    if len(have) + len(new) > MAX_PER_USER:
        raise WorkflowError(f"That would give you {len(have) + len(new)} workflows; the limit is {MAX_PER_USER}")
    return [save(db, user, wf) for wf in clean]
