"""
Spoken narration for the built-in tutorials (docs/TUTORIAL_NARRATION.md).

Each tutorial line (a step's `say`, or a pause step's text) is rendered ahead of
time to an MP3 in workflows/narration/, named by a hash of its text, so the app
never waits on a model and a changed line only re-renders that line. The player
(web/static/player.js) plays a step's clip and, in auto mode, moves on when it
ends; a line without a clip is read by the browser's own voice, or not at all.

Clips come from any text-to-speech server with OpenAI's /v1/audio/speech API.
We use Kokoro-82M (Apache-2.0) through Kokoro-FastAPI:

    AGENCY_OS_TTS_URL=http://localhost:8880/v1
    AGENCY_OS_TTS_MODEL=kokoro          (default)
    AGENCY_OS_TTS_VOICE=af_heart        (default)
    AGENCY_OS_TTS_API_KEY=              (if the server needs one)

Render with `python agency_os.py tutorials narrate`. Only the person rendering
needs the server: the clips are committed and served as static files.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Callable, Optional

import httpx

from core import workflows

NARRATION_DIR = workflows.TUTORIALS_DIR.parent / "narration"
URL_PREFIX = "/narration"


class NarrationError(RuntimeError):
    """The speech server couldn't be reached or refused a line."""


def spoken(step: dict) -> str:
    """What the player shows (and so says) for a step: a pause's text, else its caption."""
    return (step.get("pause") or step.get("say") or "").strip()


def clip_name(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:20] + ".mp3"


def with_audio(wf: dict) -> dict:
    """A copy of the workflow whose steps carry `audio` (a URL) where a clip exists."""
    steps = []
    for step in wf["steps"]:
        text = spoken(step)
        if text and (NARRATION_DIR / clip_name(text)).is_file():
            step = {**step, "audio": f"{URL_PREFIX}/{clip_name(text)}"}
        steps.append(step)
    return {**wf, "steps": steps}


def lines() -> list[str]:
    """Every line the tutorials speak, once each, in tutorial order."""
    seen: dict[str, None] = {}
    for path in sorted(workflows.TUTORIALS_DIR.glob("*.yaml")):
        for step in workflows.parse(path.read_text())["steps"]:
            if spoken(step):
                seen.setdefault(spoken(step), None)
    return list(seen)


def configured() -> bool:
    return bool(os.environ.get("AGENCY_OS_TTS_URL", "").strip())


def describe() -> str:
    return f"{_model()} voice {_voice()} at {_base_url()}"


def _base_url() -> str:
    return os.environ.get("AGENCY_OS_TTS_URL", "").strip().rstrip("/")


def _model() -> str:
    return os.environ.get("AGENCY_OS_TTS_MODEL", "").strip() or "kokoro"


def _voice() -> str:
    return os.environ.get("AGENCY_OS_TTS_VOICE", "").strip() or "af_heart"


def synthesize(text: str, client: httpx.Client) -> bytes:
    headers = {}
    if os.environ.get("AGENCY_OS_TTS_API_KEY"):
        headers["Authorization"] = f"Bearer {os.environ['AGENCY_OS_TTS_API_KEY']}"
    try:
        r = client.post(f"{_base_url()}/audio/speech", headers=headers, json={
            "model": _model(), "voice": _voice(), "input": text, "response_format": "mp3"})
    except httpx.HTTPError as exc:
        raise NarrationError(f"Couldn't reach the speech server at {_base_url()}: {exc}") from exc
    if r.status_code != 200 or not r.content:
        raise NarrationError(f"The speech server answered {r.status_code}: {r.text[:200]}")
    return r.content


def render(*, force: bool = False, client: Optional[httpx.Client] = None,
           progress: Callable[[str], None] = lambda _line: None) -> dict:
    """Render every tutorial line that has no clip yet (all of them with force),
    and delete clips no tutorial says any more. Returns counts."""
    if not configured():
        raise NarrationError("Set AGENCY_OS_TTS_URL to a speech server, e.g. http://localhost:8880/v1")
    NARRATION_DIR.mkdir(parents=True, exist_ok=True)
    wanted = {clip_name(text): text for text in lines()}
    made = kept = 0
    own = client is None
    client = client or httpx.Client(timeout=120)
    try:
        for name, text in wanted.items():
            path = NARRATION_DIR / name
            if path.is_file() and not force:
                kept += 1
                continue
            progress(text)
            tmp = path.with_suffix(".part")
            tmp.write_bytes(synthesize(text, client))
            tmp.replace(path)
            made += 1
    finally:
        if own:
            client.close()
    removed = 0
    for path in NARRATION_DIR.glob("*.mp3"):
        if path.name not in wanted:
            path.unlink()
            removed += 1
    return {"made": made, "kept": kept, "removed": removed}
