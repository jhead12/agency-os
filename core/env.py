"""
Load settings from the project's .env file at startup (CLI and web app).

Variables already set in the environment always win, so a deploy's own
settings (Railway, Docker) and anything exported in your shell are never
overridden. Set AGENCY_OS_DOTENV=off to skip the file (the test suite does,
so a developer's real keys never reach the tests).

Supports KEY=value lines, optional `export `, comments, and single or double
quotes; nothing else (no variable expansion).
"""

from __future__ import annotations

import os
import re
from pathlib import Path

ENV_FILE = Path(__file__).resolve().parent.parent / ".env"
_LINE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$")


def _value(raw: str) -> str:
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "'\"":
        return raw[1:-1]
    return re.split(r"\s+#", raw, maxsplit=1)[0].strip()  # drop a trailing " # comment"


def load_dotenv(path: Path = ENV_FILE) -> list[str]:
    """Set variables from the file that aren't already set. Returns their names."""
    if os.environ.get("AGENCY_OS_DOTENV", "").lower() == "off" or not path.is_file():
        return []
    found: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        match = _LINE.match(line) if line.strip() and not line.lstrip().startswith("#") else None
        if match:
            found[match.group(1)] = _value(match.group(2))  # a repeated key: the last one wins, as with `source`
    loaded = [k for k, v in found.items() if k not in os.environ and v != ""]
    for key in loaded:
        os.environ[key] = found[key]
    return loaded
