"""
The app's own language model: the built-in agent panel and the lead-package
AI review call generate(); the agent chat calls chat() with the conversation.

Chosen by environment:

    AGENCY_OS_LLM=anthropic          Claude (default when ANTHROPIC_API_KEY is set;
                                     needs `pip install anthropic`)
    AGENCY_OS_LLM=openai_compatible  a local or hosted OpenAI-style server, e.g.
                                     Ollama running a Hermes model:
                                     AGENCY_OS_LLM_BASE_URL=http://localhost:11434/v1
                                     AGENCY_OS_LLM_MODEL=hermes3
                                     AGENCY_OS_LLM_API_KEY=   (if the server needs one)
    AGENCY_OS_LLM=xai                xAI's Grok (default when only XAI_API_KEY is set):
                                     XAI_API_KEY=xai-...
                                     AGENCY_OS_XAI_MODEL=grok-4.6   (optional)
    AGENCY_OS_LLM=off                no model

A persona can ask for Grok whatever the app's model is (`model: grok` in its
front matter, core/agents.py): callers pass provider="xai", which needs only
XAI_API_KEY. Grok is a hosted model, so what it's sent leaves this server.

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
XAI_BASE_URL = "https://api.x.ai/v1"
XAI_DEFAULT_MODEL = "grok-4.6"
PROVIDERS = ("xai",)  # what a caller may ask for by name, besides the app's own model
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
    """"anthropic", "openai_compatible", "xai", or "" when no model is configured."""
    choice = os.environ.get("AGENCY_OS_LLM", "").strip().lower()
    if choice == "off":
        return ""
    if choice == "openai_compatible":
        return choice if _base_url() and os.environ.get("AGENCY_OS_LLM_MODEL") else ""
    if choice == "xai":
        return choice if available("xai") else ""
    if choice in ("", "anthropic") and os.environ.get("ANTHROPIC_API_KEY"):
        return "anthropic"
    if choice == "" and available("xai"):
        return "xai"
    return ""


def available(provider: str) -> bool:
    """Whether a named provider (PROVIDERS) can be called, whatever the app's own model is."""
    return provider == "xai" and bool(os.environ.get("XAI_API_KEY", "").strip())


def xai_model() -> str:
    """The Grok model to use (AGENCY_OS_XAI_MODEL)."""
    return os.environ.get("AGENCY_OS_XAI_MODEL", "").strip() or XAI_DEFAULT_MODEL


def describe(provider: str = "") -> str:
    kind = provider or backend()
    if kind == "anthropic":
        return f"Claude ({CLAUDE_MODEL})"
    if kind == "openai_compatible":
        return f"{os.environ.get('AGENCY_OS_LLM_MODEL')} at {urlsplit(_base_url()).netloc}"
    if kind == "xai":
        return f"Grok ({xai_model()})"
    return ""


def _base_url() -> str:
    url = os.environ.get("AGENCY_OS_LLM_BASE_URL", "").strip().rstrip("/")
    parts = urlsplit(url)
    return url if parts.scheme in ("http", "https") and parts.hostname else ""


def generate(system: str, prompt: str, *, max_tokens: int = 4000, effort: str = "medium",
             json_schema: Optional[dict] = None, http: Optional[httpx.Client] = None,
             provider: str = "") -> Reply:
    """One model turn: a system prompt plus one user message. Never raises.

    json_schema asks for a JSON object matching it (strictly on Claude; as a
    JSON-mode hint on OpenAI-style servers, so callers still validate).
    provider names a model to use instead of the app's own (PROVIDERS).
    """
    return chat(system, [{"role": "user", "content": prompt}], max_tokens=max_tokens, effort=effort,
                json_schema=json_schema, http=http, provider=provider)


def chat(system: str, messages: list[dict], *, max_tokens: int = 4000, effort: str = "medium",
         json_schema: Optional[dict] = None, http: Optional[httpx.Client] = None,
         provider: str = "") -> Reply:
    """The next reply in a conversation. messages alternate user/assistant and end
    with the user's turn: [{"role": "user", "content": "..."}, ...]. Never raises."""
    if provider:
        if provider not in PROVIDERS:
            return Reply(False, error=f"Unknown AI model: {provider}")
        if not available(provider):
            return Reply(False, error="Grok isn't set up on this server (XAI_API_KEY)")
        kind = provider
    else:
        kind = backend()
    if not kind:
        return Reply(False, error="No AI model is configured (AGENCY_OS_LLM)")
    try:
        if kind == "anthropic":
            return _anthropic(system, messages, max_tokens, effort, json_schema)
        if kind == "xai":
            return _openai_compatible(system, messages, max_tokens, json_schema, http, base_url=XAI_BASE_URL,
                                      model=xai_model(), api_key=os.environ["XAI_API_KEY"].strip())
        return _openai_compatible(system, messages, max_tokens, json_schema, http,
                                  base_url=_base_url(), model=os.environ["AGENCY_OS_LLM_MODEL"],
                                  api_key=os.environ.get("AGENCY_OS_LLM_API_KEY", ""))
    except Exception as exc:  # a model outage must never take a page down
        return Reply(False, error=f"The AI model failed: {type(exc).__name__}: {str(exc)[:200]}")


def _anthropic(system: str, messages: list[dict], max_tokens: int, effort: str, json_schema: Optional[dict]) -> Reply:
    import anthropic

    output_config: dict = {"effort": effort}
    if json_schema:
        output_config["format"] = {"type": "json_schema", "schema": json_schema}
    response = anthropic.Anthropic().messages.create(
        model=CLAUDE_MODEL,
        max_tokens=max_tokens,
        # The persona is the long, repeated part; cache it across requests.
        system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
        messages=messages,
        output_config=output_config,
    )
    if response.stop_reason == "refusal":
        return Reply(False, error="The model declined this request")
    text = "".join(block.text for block in response.content if block.type == "text")
    if response.stop_reason == "max_tokens":
        return Reply(False, text=text, error="The reply was cut off (too long)")
    return Reply(True, text=text)


def _openai_compatible(system: str, messages: list[dict], max_tokens: int, json_schema: Optional[dict],
                       http: Optional[httpx.Client], *, base_url: str, model: str, api_key: str) -> Reply:
    body: dict = {
        "model": model,
        "messages": [{"role": "system", "content": system}, *messages],
        "max_tokens": max_tokens,
    }
    if json_schema:
        body["response_format"] = {"type": "json_object"}
    headers = {}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    client = http or httpx.Client(timeout=_TIMEOUT, follow_redirects=False)
    try:
        response = client.post(f"{base_url}/chat/completions", json=body, headers=headers)
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
