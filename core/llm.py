"""
The app's own language model: the built-in agent panel and the lead-package
AI review both call generate().

Chosen by environment:

    AGENCY_OS_LLM=anthropic          Claude (default when ANTHROPIC_API_KEY is set;
                                     needs `pip install anthropic`)
    AGENCY_OS_LLM=openai_compatible  a local or hosted OpenAI-style server, e.g.
                                     Ollama running a Hermes model:
                                     AGENCY_OS_LLM_BASE_URL=http://localhost:11434/v1
                                     AGENCY_OS_LLM_MODEL=hermes3
                                     AGENCY_OS_LLM_API_KEY=   (if the server needs one)
    AGENCY_OS_LLM=off                no model

A hosted deploy can't reach a model on someone's laptop; a local model is for
self-hosted or local runs. generate() never raises.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlsplit

import httpx

CLAUDE_MODEL = "claude-opus-5-5"
_TIMEOUT = httpx.Timeout(180.0, connect=10.0)


@dataclass
class Reply:
    ok: bool
    text: str = ""
    error: str = ""

    def json(self) -> Optional[dict]:
        """The reply parsed as a JSON object, or None."""
        text = self.text.strip()
        if text.startswith("```"):
            text = text.strip("`").removeprefix("json").strip()
        try:
            data = json.loads(text)
        except ValueError:
            return None
        return data if isinstance(data, dict) else None


def backend() -> str:
    """"anthropic", "openai_compatible", or "" when no model is configured."""
    choice = os.environ.get("AGENCY_OS_LLM", "").strip().lower()
    if choice == "off":
        return ""
    if choice == "openai_compatible":
        return choice if _base_url() and os.environ.get("AGENCY_OS_LLM_MODEL") else ""
    if choice in ("", "anthropic") and os.environ.get("ANTHROPIC_API_KEY"):
        return "anthropic"
    return ""


def describe() -> str:
    kind = backend()
    if kind == "anthropic":
        return f"Claude ({CLAUDE_MODEL})"
    if kind == "openai_compatible":
        return f"{os.environ.get('AGENCY_OS_LLM_MODEL')} at {urlsplit(_base_url()).netloc}"
    return ""


def _base_url() -> str:
    url = os.environ.get("AGENCY_OS_LLM_BASE_URL", "").strip().rstrip("/")
    parts = urlsplit(url)
    return url if parts.scheme in ("http", "https") and parts.hostname else ""


def generate(system: str, prompt: str, *, max_tokens: int = 4000, effort: str = "medium",
             json_schema: Optional[dict] = None, http: Optional[httpx.Client] = None) -> Reply:
    """One model turn: a system prompt plus one user message. Never raises.

    json_schema asks for a JSON object matching it (strictly on Claude; as a
    JSON-mode hint on OpenAI-style servers, so callers still validate).
    """
    kind = backend()
    if not kind:
        return Reply(False, error="No AI model is configured (AGENCY_OS_LLM)")
    try:
        if kind == "anthropic":
            return _anthropic(system, prompt, max_tokens, effort, json_schema)
        return _openai_compatible(system, prompt, max_tokens, json_schema, http)
    except Exception as exc:  # a model outage must never take a page down
        return Reply(False, error=f"The AI model failed: {type(exc).__name__}: {str(exc)[:200]}")


def _anthropic(system: str, prompt: str, max_tokens: int, effort: str, json_schema: Optional[dict]) -> Reply:
    import anthropic

    output_config: dict = {"effort": effort}
    if json_schema:
        output_config["format"] = {"type": "json_schema", "schema": json_schema}
    response = anthropic.Anthropic().messages.create(
        model=CLAUDE_MODEL,
        max_tokens=max_tokens,
        # The persona is the long, repeated part; cache it across requests.
        system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": prompt}],
        output_config=output_config,
    )
    if response.stop_reason == "refusal":
        return Reply(False, error="The model declined this request")
    text = "".join(block.text for block in response.content if block.type == "text")
    if response.stop_reason == "max_tokens":
        return Reply(False, text=text, error="The reply was cut off (too long)")
    return Reply(True, text=text)


def _openai_compatible(system: str, prompt: str, max_tokens: int, json_schema: Optional[dict],
                       http: Optional[httpx.Client]) -> Reply:
    body: dict = {
        "model": os.environ["AGENCY_OS_LLM_MODEL"],
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
    }
    if json_schema:
        body["response_format"] = {"type": "json_object"}
    headers = {}
    if os.environ.get("AGENCY_OS_LLM_API_KEY"):
        headers["Authorization"] = f"Bearer {os.environ['AGENCY_OS_LLM_API_KEY']}"
    client = http or httpx.Client(timeout=_TIMEOUT, follow_redirects=False)
    try:
        response = client.post(f"{_base_url()}/chat/completions", json=body, headers=headers)
    finally:
        if http is None:
            client.close()
    if not response.is_success:
        return Reply(False, error=f"The AI model returned {response.status_code}")
    choice = (response.json().get("choices") or [{}])[0]
    text = (choice.get("message") or {}).get("content") or ""
    if choice.get("finish_reason") == "length":
        return Reply(False, text=text, error="The reply was cut off (too long)")
    return Reply(bool(text), text=text, error="" if text else "The model returned nothing")
