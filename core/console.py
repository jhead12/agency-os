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

Errors teach: each one says what went wrong and how to fix it — the closest
command or flag, the command's usage and a ready-to-run example, the role a
command needs, or a command that helps (e.g. search-prospects to find an id).
"""

from __future__ import annotations

import difflib
import re
import shlex

from core import access, llm, tools
from core.access import CurrentUser

MAX_LINE = 4000
BUILTINS = {"help": "This list", "whoami": "Who you're signed in as", "clear": "Clear the screen"}
SHELL_WORDS = {"ls", "cd", "rm", "cat", "sudo", "curl", "wget", "python", "python3", "pip", "git", "bash",
               "sh", "echo", "mkdir", "cp", "mv", "chmod", "kill", "ps", "top", "vim", "nano", "exit", "quit"}
# Example values for flags, by name; anything else gets a placeholder.
SAMPLES = {"campaign": "voter-guide-cbo", "user": "jane@example.com", "email": "jane@example.com", "name": '"Jane Doe"', "prospect_id": "42", "q": '"food bank"',
           "note": '"Left a voicemail"', "notes": '"Asked for a callback"', "role": "Caller",
           "limit": "10", "decision_maker_name": '"Pat Lee"', "instructions": '"Keep it short"'}
# Hints for errors a command returns: (text in the error, command that helps, what it's for).
HINTS = [
    ("Prospect not found", 'search-prospects --q "<name>"', "find a prospect's id"),
    ("isn't in a campaign", "get-prospect --prospect-id <id>", "see which campaigns a prospect is in"),
    ("Campaign not found", 'search-prospects --q "<name>"', "see campaign names next to prospects"),
    ("Unknown agent", "list-agent-personas", "see the agents you can use"),
    ("already exists", "users list", "see who's already on the team"),
    ("No user with email", "users list", "see team members' emails"),
    ("Only a Super Admin", "users list", "see who the Super Admins are"),
]


GROUPS = ("users", "workflows", "campaigns")  # commands typed as "<group> <action>"


def _command(tool: tools.Tool) -> str:
    """users_create_owner → "users create-owner"; search_prospects → "search-prospects"."""
    group = tool.name.split("_", 1)[0]
    if group in GROUPS:
        return f"{group} " + tool.name.removeprefix(group + "_").replace("_", "-")
    return tool.name.replace("_", "-")


def _console_tools() -> list[tools.Tool]:
    return [t for t in tools.TOOLS.values() if "console" in t.surfaces]


def _resolve(words: list[str], user: CurrentUser | None) -> tuple[tools.Tool | None, list[str]]:
    """Match the longest leading words to a tool this user may run (any console tool if user is None)."""
    pool = tools.available(user, "console") if user else _console_tools()
    by_name = {t.name: t for t in pool}
    for n in (2, 1):
        if len(words) >= n:
            name = "_".join(words[:n]).replace("-", "_")
            if name in by_name:
                return by_name[name], words[n:]
    return None, words


# ── Teaching: usage, examples and friendly errors ──────────────────────


def _flag(key: str) -> str:
    return "--" + key.replace("_", "-")


def _props(tool: tools.Tool) -> dict:
    return tool.input_schema.get("properties", {})


def _required(tool: tools.Tool) -> list[str]:
    return list(tool.input_schema.get("required", []))


def _placeholder(key: str, schema: dict) -> str:
    if schema.get("type") == "integer":
        return "<number>" if key != "prospect_id" else "<id>"
    return f"<{key.replace('_', '-')}>"


def _sample(key: str, schema: dict) -> str:
    if "enum" in schema:
        return str(schema["enum"][0])
    return SAMPLES.get(key) or ("10" if schema.get("type") == "integer" else _placeholder(key, schema))


def usage(tool: tools.Tool) -> str:
    """e.g. users invite --email <email> [--name <name>] [--role <role>]... [--no-send]"""
    required = _required(tool)
    parts = [_command(tool)]
    for key in sorted(_props(tool), key=lambda k: k not in required):
        schema = _props(tool)[key]
        piece = _flag(key) if schema.get("type") == "boolean" else f"{_flag(key)} {_placeholder(key, schema)}"
        piece = piece if key in required else f"[{piece}]"
        parts.append(piece + ("..." if schema.get("type") == "array" else ""))
    return " ".join(parts)


def example(tool: tools.Tool) -> str:
    """A runnable line: the required flags with sample values (or the first flag if none are required)."""
    keys = _required(tool) or list(_props(tool))[:1]
    return " ".join([_command(tool)] + [f"{_flag(k)} {_sample(k, _props(tool)[k])}" for k in keys])


def _guide(tool: tools.Tool, problem: str) -> str:
    return "\n".join([f"Error: {problem}", f"  Usage:   {usage(tool)}", f"  Example: {example(tool)}",
                      f"  More:    help {_command(tool)}"])


def _needs(tool: tools.Tool) -> str:
    """Who may run a tool, in words."""
    if tool.permission == access.SUPER_ADMIN:
        return "the Super Admin role"
    if tool.permission == access.OWNER:
        return "the Owner role"
    label = access.CATALOG.get(tool.permission, "")
    return f"the {tool.permission} permission" + (f" ({label[0].lower()}{label[1:]})" if label else "")


def _not_allowed(what: str, tool: tools.Tool, user: CurrentUser) -> str:
    ask = "a Super Admin" if tool.permission == access.SUPER_ADMIN else "an Owner"
    verb = "need" if what.endswith("commands") else "needs"
    return (f"Error: {what} {verb} {_needs(tool)}. You have: {', '.join(user.roles) or 'no roles'}.\n"
            f"  Ask {ask} if you need it. Type help to see what you can run.")


def _suggest(typed: str, choices: list[str]) -> list[str]:
    return difflib.get_close_matches(typed, choices, n=3, cutoff=0.6)


def _unknown_command(words: list[str], user: CurrentUser) -> str:
    """Why a line didn't match a command this user can run, and what to type instead."""
    word = words[0]
    if word in SHELL_WORDS:
        return (f"Error: '{word}' is a shell command. This console runs agency-os commands only (it isn't a "
                f"server shell).\n  Type help to see what you can run.")

    # A real command this user can't run: say what it needs.
    other, _ = _resolve(words, None)
    if other is not None:
        if other.kind == "draft" and user.can(other.permission):
            reason = ("no AI model is set up on this server" if not llm.backend()
                      else "AI features are off for you; turn them on under Account → AI features")
            return f"Error: {_command(other)} drafts with AI, and {reason}."
        return _not_allowed(_command(other), other, user)

    mine = tools.available(user, "console")
    names = sorted(BUILTINS) + [_command(t) for t in mine]
    # A group word on its own, e.g. "users".
    group = [n for n in names if n.startswith(word + " ")]
    if group and (len(words) == 1 or words[1].startswith("--")):
        return f"Error: {word} needs a subcommand: " + ", ".join(group) + f".\n  Example: {group[0]}"
    hidden_group = [t for t in _console_tools() if t.name.startswith(word.replace("-", "_") + "_")]
    if hidden_group and not group:
        return _not_allowed(f"the {word} commands", hidden_group[0], user)

    typed = " ".join(words[:2]) if len(words) > 1 and not words[1].startswith("--") else word
    close = _suggest(typed, names) or _suggest(word, names)
    hint = f" Did you mean: {', '.join(close)}?" if close else ""
    return f"Error: Unknown command '{typed}'.{hint}\n  Type help to see the {len(names)} commands you can run."


def _parse_flags(tool: tools.Tool, words: list[str]) -> dict:
    props = _props(tool)
    args: dict = {}
    i = 0
    while i < len(words):
        word = words[i]
        if not word.startswith("--"):
            takes_value = [k for k in _required(tool) if props[k].get("type") != "boolean" and k not in args]
            if word.startswith("-") and len(word) > 1:
                raise tools.ToolError(f"Flags start with two dashes: --{word.lstrip('-')}")
            if len(takes_value) == 1:
                k = takes_value[0]
                raise tools.ToolError(f"'{word}' needs a flag in front of it. "
                                      f"Did you mean: {_command(tool)} {_flag(k)} {shlex.quote(word)}?")
            raise tools.ToolError(f"'{word}' needs a flag in front of it, like {_flag(next(iter(props), 'name'))} "
                                  f"{shlex.quote(word)}. Values with spaces need quotes.")
        key, _, inline = word[2:].partition("=")
        key = key.replace("-", "_")
        schema = props.get(key)
        if schema is None:
            close = _suggest(key, list(props))
            hint = f" Did you mean {_flag(close[0])}?" if close else ""
            takes = ", ".join(_flag(k) for k in props) or "no flags"
            raise tools.ToolError(f"Unknown flag {_flag(key)}.{hint} {_command(tool)} takes: {takes}.")
        if schema.get("type") == "boolean":
            args[key] = inline.lower() not in ("false", "0", "no") if inline else True
            i += 1
            continue
        if inline:
            value = inline
        elif i + 1 < len(words) and not words[i + 1].startswith("--"):
            value, i = words[i + 1], i + 1
        else:
            raise tools.ToolError(f"{_flag(key)} needs a value, like {_flag(key)} {_sample(key, schema)}")
        if schema.get("type") == "array":
            args.setdefault(key, []).append(value)
        else:
            args[key] = value
        i += 1
    missing = [k for k in _required(tool) if k not in args]
    if missing:
        raise tools.ToolError(f"{_command(tool)} needs {' and '.join(_flag(k) for k in missing)}.")
    return args


def _explain(tool: tools.Tool, error: str, user: CurrentUser) -> str:
    """A tool's error, with usage for bad input or a command that helps."""
    # Bad input names an argument ("prospect_id must be a whole number"): show it as a flag, with usage.
    match = re.match(r"(\w+)(\[\d+\])? ", error)
    if match and match.group(1) in _props(tool):
        return _guide(tool, _flag(match.group(1)) + error[len(match.group(1)):])
    for text, command, purpose in HINTS:
        if text in error and _resolve(command.split(), user)[0] is not None:
            return f"Error: {error}\n  To {purpose}: {command}"
    return f"Error: {error}"


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
        if topic[0] in BUILTINS and len(topic) == 1:
            return f"{topic[0]}\n  {BUILTINS[topic[0]]}"
        tool, rest = _resolve(topic, user)
        if tool is None or rest:
            return _unknown_command(topic, user).replace("Error: ", "", 1)
        props = tool.input_schema.get("properties", {})
        required = set(tool.input_schema.get("required", []))
        lines = [_command(tool), f"  {tool.description}", "", f"  Usage:   {usage(tool)}",
                 f"  Example: {example(tool)}", "", "  Flags:"]
        for key, schema in props.items():
            flag = f"--{key.replace('_', '-')}"
            kind = schema.get("type")
            hint = "" if kind == "boolean" else f" {_placeholder(key, schema)}"
            notes = [schema.get("description", "")]
            if "enum" in schema:
                notes.append("one of: " + ", ".join(map(str, schema["enum"])))
            if kind == "array" and "repeat" not in schema.get("description", ""):
                notes.append("repeatable")
            if key in required:
                notes.append("required")
            lines.append(f"    {flag + hint:<28} {'; '.join(n for n in notes if n)}")
        if not props:
            lines.append("    (none)")
        if tool.kind == "write":
            lines += ["", "  Makes a change; you'll be asked to confirm."]
        return "\n".join(lines)
    mine = tools.available(user, "console")
    width = max((len(_command(t)) for t in mine), default=10)
    lines = ["Commands (help <command> for its flags and an example):", ""]
    lines += [f"  {name:<{width}}  {text}" for name, text in BUILTINS.items()]
    lines += [f"  {_command(t):<{width}}  {t.description}" for t in sorted(mine, key=_command)]
    lines += ["", 'Flags go after the command: --name value. Quote values with spaces: --name "Jane Doe".',
              "Changes ask Run this? [y/N] before they happen."]
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
    if words == ["clear"]:  # the browser clears itself; the remote CLI has nothing to clear
        return {"ok": True, "output": ""}

    tool, rest = _resolve(words, user)
    if tool is None:
        return {"ok": False, "output": _unknown_command(words, user)}
    try:
        args = _parse_flags(tool, rest)
    except tools.ToolError as exc:
        return {"ok": False, "output": _guide(tool, str(exc))}
    try:
        tools.validate(tool.input_schema, args)  # bad values are reported before asking to confirm
    except tools.ToolError as exc:
        return {"ok": False, "output": _explain(tool, str(exc), user)}
    if tool.kind == "write" and not confirmed:
        return {"ok": False, "needs_confirmation": True, "output": f"{_command(tool)}: {tool.description}"}
    result = tools.run_tool(db, user, tool.name, args, source=source, confirmed=confirmed, surface="console")
    if not result.get("ok"):
        return {"ok": False, "output": _explain(tool, result.get("error", "failed"), user)}
    play = result.pop("play", None)
    if play is not None:  # the browser's player acts it out
        if source == "cli":
            return {"ok": True, "output": f"{result['playing']} plays in the browser: open agency-os and run "
                                          f"this from the console window (Ctrl+`)."}
        return {"ok": True, "output": f"Playing {result['playing']}. Watch the page; the controls are at the bottom.",
                "play": play}
    return {"ok": True, "output": format_result(result)}
