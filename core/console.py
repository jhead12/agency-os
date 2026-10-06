"""
The command console: one command language for the browser terminal
(/console) and the remote CLI (`agency_os.py remote`).

It is not a shell. A line is split like a shell would (quotes work) and runs
either a built-in (help, whoami) or a tool from core/tools.py on the
"console" surface, as the signed-in user, with that user's permissions:

    users invite --email jane@example.com --name "Jane" --role Caller
    search-prospects --q "food bank" --limit 5
    set-stage --prospect-id 42 --stage engaged

A tool `users_invite` is typed `users invite` or `users-invite`; its flags come
from its JSON Schema (`--prospect-id` → prospect_id, a repeated flag fills a
list, a boolean flag takes no value). New tools appear here automatically.
Changes (write tools) ask "Run this? [y/N]" before they run.
"""

from __future__ import annotations

import shlex

from core import tools
from core.access import CurrentUser

MAX_LINE = 4000


def _command(tool: tools.Tool) -> str:
    """users_create_owner → "users create-owner"; search_prospects → "search-prospects"."""
    if tool.name.startswith("users_"):
        return "users " + tool.name.removeprefix("users_").replace("_", "-")
    return tool.name.replace("_", "-")


def _resolve(words: list[str], user: CurrentUser) -> tuple[tools.Tool | None, list[str]]:
    """Match the longest leading words to a tool this user may run."""
    mine = {t.name: t for t in tools.available(user, "console")}
    for n in (2, 1):
        if len(words) >= n:
            name = "_".join(words[:n]).replace("-", "_")
            if name in mine:
                return mine[name], words[n:]
    return None, words


def _parse_flags(tool: tools.Tool, words: list[str]) -> dict:
    props = tool.input_schema.get("properties", {})
    args: dict = {}
    i = 0
    while i < len(words):
        word = words[i]
        if not word.startswith("--"):
            raise tools.ToolError(f"Unexpected '{word}'. Flags look like --name value; try: help {_command(tool)}")
        key, _, inline = word[2:].partition("=")
        key = key.replace("-", "_")
        schema = props.get(key)
        if schema is None:
            raise tools.ToolError(f"Unknown flag --{key.replace('_', '-')}; try: help {_command(tool)}")
        if schema.get("type") == "boolean":
            args[key] = inline.lower() not in ("false", "0", "no") if inline else True
            i += 1
            continue
        if inline:
            value = inline
        elif i + 1 < len(words):
            value, i = words[i + 1], i + 1
        else:
            raise tools.ToolError(f"--{key.replace('_', '-')} needs a value")
        if schema.get("type") == "array":
            args.setdefault(key, []).append(value)
        else:
            args[key] = value
        i += 1
    return args


# ── Output ─────────────────────────────────────────────────────────────


def _cell(value) -> str:
    text = "—" if value is None or value == "" else str(value)
    return text if len(text) <= 60 else text[:57] + "..."


def _table(rows: list[dict]) -> str:
    if not rows:
        return "(none)"
    cols = list(rows[0])
    cells = [[_cell(r.get(c)) for c in cols] for r in rows]
    widths = [max(len(c), *(len(row[i]) for row in cells)) for i, c in enumerate(cols)]
    line = lambda vals: "  ".join(v.ljust(w) for v, w in zip(vals, widths)).rstrip()  # noqa: E731
    return "\n".join([line(cols), line(["-" * w for w in widths])] + [line(r) for r in cells])


def format_result(result: dict) -> str:
    """Plain text for a tool result: lists of records as tables, the rest as key: value."""
    parts = []
    for key, value in result.items():
        if key == "ok":
            continue
        if isinstance(value, list) and all(isinstance(v, dict) for v in value):
            parts.append(f"{key}:\n{_table(value)}")
        elif isinstance(value, dict):
            parts.append(f"{key}:\n" + "\n".join(f"  {k}: {_cell(v)}" for k, v in value.items()))
        elif isinstance(value, str) and "\n" in value:
            parts.append(value)
        else:
            parts.append(f"{key}: {value}")
    return "\n".join(parts) or "Done."


def _help(user: CurrentUser, topic: list[str]) -> str:
    if topic:
        tool, rest = _resolve(topic, user)
        if tool is None or rest:
            return f"No command '{' '.join(topic)}' for you. Type help to list yours."
        props = tool.input_schema.get("properties", {})
        required = set(tool.input_schema.get("required", []))
        lines = [_command(tool), f"  {tool.description}", ""]
        for key, schema in props.items():
            flag = f"--{key.replace('_', '-')}"
            kind = schema.get("type")
            hint = "" if kind == "boolean" else f" <{'value' if kind != 'integer' else 'number'}>"
            notes = [schema.get("description", "")]
            if "enum" in schema:
                notes.append("one of: " + ", ".join(map(str, schema["enum"])))
            if kind == "array":
                notes.append("repeatable")
            if key in required:
                notes.append("required")
            lines.append(f"  {flag + hint:<30} {'; '.join(n for n in notes if n)}")
        if not props:
            lines.append("  (no flags)")
        if tool.kind == "write":
            lines += ["", "  Makes a change; you'll be asked to confirm."]
        return "\n".join(lines)
    mine = tools.available(user, "console")
    width = max((len(_command(t)) for t in mine), default=10)
    lines = ["Commands (help <command> for its flags):", "",
             f"  {'help':<{width}}  This list", f"  {'whoami':<{width}}  Who you're signed in as",
             f"  {'clear':<{width}}  Clear the screen"]
    lines += [f"  {_command(t):<{width}}  {t.description}" for t in sorted(mine, key=_command)]
    return "\n".join(lines)


# ── Running a line ────────────────────────────────────────────────────


def run_line(db, user: CurrentUser, line: str, *, confirmed: bool = False, source: str = "console") -> dict:
    """Run one console line as `user`. Returns {ok, output[, needs_confirmation]}; never raises."""
    line = (line or "").strip()
    if len(line) > MAX_LINE:
        return {"ok": False, "output": "That line is too long."}
    try:
        words = shlex.split(line)
    except ValueError as exc:
        return {"ok": False, "output": f"Couldn't read that line: {exc}"}
    if not words:
        return {"ok": True, "output": ""}
    if words[0] == "help":
        return {"ok": True, "output": _help(user, words[1:])}
    if words == ["whoami"]:
        return {"ok": True, "output": f"{user.name} <{user.email}>\nroles: {', '.join(user.roles) or '(none)'}"}

    tool, rest = _resolve(words, user)
    if tool is None:
        return {"ok": False, "output": f"Unknown command '{words[0]}'. Type help to see what you can run."}
    try:
        args = _parse_flags(tool, rest)
    except tools.ToolError as exc:
        return {"ok": False, "output": str(exc)}
    if tool.kind == "write" and not confirmed:
        return {"ok": False, "needs_confirmation": True, "output": f"{_command(tool)}: {tool.description}"}
    result = tools.run_tool(db, user, tool.name, args, source=source, confirmed=confirmed, surface="console")
    if not result.get("ok"):
        return {"ok": False, "output": f"Error: {result.get('error', 'failed')}"}
    return {"ok": True, "output": format_result(result)}
