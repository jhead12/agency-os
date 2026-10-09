"""
Tutorial narration (core/narration.py): which line each step says, rendering
clips through an OpenAI-style speech server, and handing clips to the player.
No speech server or database needed.

Run: python -m pytest tests/test_narration.py
"""

import json
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import narration  # noqa: E402


@pytest.fixture
def clips(tmp_path, monkeypatch):
    monkeypatch.setattr(narration, "NARRATION_DIR", tmp_path / "narration")
    monkeypatch.setenv("AGENCY_OS_TTS_URL", "http://tts.test/v1/")
    monkeypatch.delenv("AGENCY_OS_TTS_MODEL", raising=False)
    monkeypatch.delenv("AGENCY_OS_TTS_VOICE", raising=False)
    monkeypatch.delenv("AGENCY_OS_TTS_API_KEY", raising=False)
    return tmp_path / "narration"


def speech_server(calls):
    def handle(request):
        calls.append((request.url.path, request.headers.get("authorization"), json.loads(request.content)))
        return httpx.Response(200, content=b"ID3 " + json.loads(request.content)["input"].encode())
    return httpx.Client(transport=httpx.MockTransport(handle))


def test_a_step_says_its_pause_text_else_its_caption():
    assert narration.spoken({"pause": "Your turn.", "say": "ignored"}) == "Your turn."
    assert narration.spoken({"pause": "", "say": "Then this."}) == "Then this."
    assert narration.spoken({"highlight": ".x", "say": " Look here. "}) == "Look here."
    assert narration.spoken({"goto": "/prospects"}) == ""


def test_every_tutorial_has_lines_to_say():
    lines = narration.lines()
    assert len(lines) == len(set(lines)) > 50
    assert all(line == line.strip() and line for line in lines)


def test_render_makes_missing_clips_keeps_the_rest_and_removes_stale_ones(clips):
    calls = []
    first = narration.render(client=speech_server(calls))
    assert first == {"made": len(narration.lines()), "kept": 0, "removed": 0}
    path, auth, body = calls[0]
    assert path == "/v1/audio/speech" and auth is None
    assert body == {"model": "kokoro", "voice": "af_heart", "input": narration.lines()[0], "response_format": "mp3"}
    assert (clips / narration.clip_name(narration.lines()[0])).read_bytes() == b"ID3 " + narration.lines()[0].encode()

    (clips / "0000stale.mp3").write_bytes(b"old line")
    (clips / narration.clip_name(narration.lines()[1])).unlink()
    calls.clear()
    again = narration.render(client=speech_server(calls))
    assert again == {"made": 1, "kept": len(narration.lines()) - 1, "removed": 1}
    assert [c[2]["input"] for c in calls] == [narration.lines()[1]]
    assert not list(clips.glob("*.part"))


def test_force_renders_everything_with_the_chosen_voice_and_key(clips, monkeypatch):
    narration.render(client=speech_server([]))
    monkeypatch.setenv("AGENCY_OS_TTS_VOICE", "bf_emma")
    monkeypatch.setenv("AGENCY_OS_TTS_API_KEY", "sk-test")
    calls = []
    assert narration.render(force=True, client=speech_server(calls))["made"] == len(narration.lines())
    assert {c[1] for c in calls} == {"Bearer sk-test"} and {c[2]["voice"] for c in calls} == {"bf_emma"}


def test_render_explains_what_is_wrong(clips, monkeypatch):
    refuse = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(400, text="unknown voice")))
    with pytest.raises(narration.NarrationError, match="answered 400: unknown voice"):
        narration.render(client=refuse)

    def down(request):
        raise httpx.ConnectError("refused")
    with pytest.raises(narration.NarrationError, match="Couldn't reach the speech server at http://tts.test/v1"):
        narration.render(client=httpx.Client(transport=httpx.MockTransport(down)))

    monkeypatch.delenv("AGENCY_OS_TTS_URL")
    with pytest.raises(narration.NarrationError, match="Set AGENCY_OS_TTS_URL"):
        narration.render()


def test_the_player_gets_a_clip_only_for_lines_that_have_one(clips):
    wf = {"name": "x", "steps": [{"goto": "/prospects", "say": "Rendered."}, {"say": "Not yet."},
                                 {"highlight": ".x"}, {"pause": "Rendered."}]}
    clips.mkdir()
    (clips / narration.clip_name("Rendered.")).write_bytes(b"ID3")
    steps = narration.with_audio(wf)["steps"]
    clip = f"/narration/{narration.clip_name('Rendered.')}"
    assert [s.get("audio") for s in steps] == [clip, None, None, clip]
    assert "audio" not in wf["steps"][0]  # the tutorial itself is untouched
