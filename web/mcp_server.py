"""
agency-os as a remote MCP server: POST /mcp (streamable HTTP), for Hermes
Agent, Claude Desktop/Code, Rook and other MCP clients (personal tokens), and
Claude.ai / ChatGPT connectors (OAuth). See core/mcp_auth.py for sign-in.

It serves the same tools as the agent panel and WebMCP (core/tools.py):

- tools: the ones the token's user may call. Changes (write tools) appear only
  for tokens allowed to make changes; the client's own approval prompt is the
  confirmation, and every change is audited as "mcp:<token or app name>".
- prompts: one per persona and task (e.g. sales-outbound-strategist.next_email),
  so the user's own model does the drafting at no cost to the server.
- resources: personas (agency://agents/<key>) and prospects (agency://prospects/<id>).

Stateless with JSON responses, so any worker can answer any request. A token
only works while its user has AI features on (Account page) and AGENCY_OS_AI
isn't off.
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from typing import Callable, Optional
from urllib.parse import urlsplit

import anyio
from mcp import types
from mcp.server.auth.provider import ProviderTokenVerifier
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
from mcp.server.lowlevel import Server
from mcp.server.transport_security import TransportSecuritySettings
from starlette.responses import JSONResponse

from core import agents, tools
from core.mcp_auth import READ, SCOPES, WRITE, AgencyOAuthProvider

_AI_OFF = "AI features are off for this account. Turn them on under Account → AI features in agency-os."


class _Caller:
    def __init__(self, user, scopes: list[str], label: str):
        self.user, self.scopes, self.label = user, scopes, label

    @property
    def can_write(self) -> bool:
        return WRITE in self.scopes


def _text(data) -> list[types.TextContent]:
    return [types.TextContent(type="text", text=json.dumps(data, default=str))]


def build_server(get_db: Callable) -> Server:
    def caller(ctx) -> Optional[_Caller]:
        request = getattr(ctx, "request", None)
        token = getattr(getattr(request, "user", None), "access_token", None) if request else None
        if token is None or not token.subject:
            return None
        user = get_db().load_current_user(int(token.subject))
        if user is None or not user.uses_ai("ai.connect"):
            return None
        label = (token.claims or {}).get("token_name") or token.client_id
        return _Caller(user, token.scopes, str(label)[:80])

    async def who(ctx) -> Optional[_Caller]:
        return await anyio.to_thread.run_sync(caller, ctx)

    def visible(c: _Caller) -> list[tools.Tool]:
        return [t for t in tools.available(c.user) if t.kind != "write" or c.can_write]

    async def list_tools(ctx, params) -> types.ListToolsResult:
        c = await who(ctx)
        if c is None:
            return types.ListToolsResult(tools=[])
        return types.ListToolsResult(tools=[
            types.Tool(name=t.name, description=t.description, input_schema=t.input_schema,
                       annotations=types.ToolAnnotations(read_only_hint=t.kind != "write",
                                                         destructive_hint=False if t.kind != "write" else None,
                                                         idempotent_hint=t.kind == "read"))
            for t in visible(c)])

    async def call_tool(ctx, params: types.CallToolRequestParams) -> types.CallToolResult:
        c = await who(ctx)
        if c is None:
            return types.CallToolResult(content=_text({"ok": False, "error": _AI_OFF}), is_error=True)
        tool = tools.TOOLS.get(params.name)
        if tool is None or tool not in visible(c):
            error = "This token can't make changes" if tool and tool.kind == "write" else f"No tool named {params.name}"
            return types.CallToolResult(content=_text({"ok": False, "error": error}), is_error=True)
        result = await anyio.to_thread.run_sync(
            lambda: tools.run_tool(get_db(), c.user, params.name, dict(params.arguments or {}),
                                   source=f"mcp:{c.label}", confirmed=c.can_write))
        return types.CallToolResult(content=_text(result), structured_content=result, is_error=not result.get("ok"))

    # Prompts: persona x task, run by the user's own model

    def prompt_name(persona, task: str) -> str:
        return f"{persona.key}.{task}"

    async def list_prompts(ctx, params) -> types.ListPromptsResult:
        c = await who(ctx)
        if c is None or not c.user.can("agents.use"):
            return types.ListPromptsResult(prompts=[])
        return types.ListPromptsResult(prompts=[
            types.Prompt(name=prompt_name(p, task), title=f"{p.name}: {p.task(task)[0]}",
                         description=p.description,
                         arguments=[types.PromptArgument(name="prospect_id", description="The prospect's id", required=True),
                                    types.PromptArgument(name="instructions", description="Anything to add", required=False)])
            for p in agents.load_personas().values() for task in p.tasks])

    async def get_prompt(ctx, params: types.GetPromptRequestParams) -> types.GetPromptResult:
        c = await who(ctx)
        if c is None or not c.user.can("agents.use"):
            raise ValueError(_AI_OFF)
        key, _, task = params.name.rpartition(".")
        persona = agents.load_personas().get(key)
        if persona is None or task not in persona.tasks:
            raise ValueError(f"Unknown prompt: {params.name}")
        args = params.arguments or {}
        try:
            prospect_id = int(args.get("prospect_id", ""))
        except ValueError:
            raise ValueError("prospect_id must be a number") from None
        context = await anyio.to_thread.run_sync(lambda: tools.build_prospect_context(get_db(), c.user, prospect_id))
        system, user = agents.build_prompt(persona, task, context, args.get("instructions", ""))
        return types.GetPromptResult(description=f"{persona.name}: {persona.task(task)[0]}", messages=[
            types.PromptMessage(role="user", content=types.TextContent(type="text", text=f"{system}\n\n---\n\n{user}"))])

    # Resources: personas and prospects

    async def list_resources(ctx, params) -> types.ListResourcesResult:
        c = await who(ctx)
        if c is None or not c.user.can("agents.use"):
            return types.ListResourcesResult(resources=[])
        return types.ListResourcesResult(resources=[
            types.Resource(name=p.key, title=p.name, uri=f"agency://agents/{p.key}", description=p.description,
                           mime_type="text/markdown") for p in agents.load_personas().values()])

    async def list_templates(ctx, params) -> types.ListResourceTemplatesResult:
        return types.ListResourceTemplatesResult(resource_templates=[
            types.ResourceTemplate(name="prospect", uri_template="agency://prospects/{prospect_id}",
                                   description="Everything known about one prospect", mime_type="application/json")])

    async def read_resource(ctx, params: types.ReadResourceRequestParams) -> types.ReadResourceResult:
        c = await who(ctx)
        if c is None:
            raise ValueError(_AI_OFF)
        uri = str(params.uri)
        if uri.startswith("agency://agents/") and c.user.can("agents.use"):
            persona = agents.load_personas().get(uri.removeprefix("agency://agents/"))
            if persona:
                return types.ReadResourceResult(contents=[
                    types.TextResourceContents(uri=uri, mime_type="text/markdown", text=persona.body)])
        if uri.startswith("agency://prospects/"):
            result = await anyio.to_thread.run_sync(lambda: tools.run_tool(
                get_db(), c.user, "get_prospect", {"prospect_id": uri.removeprefix("agency://prospects/")},
                source=f"mcp:{c.label}"))
            if result.get("ok"):
                return types.ReadResourceResult(contents=[types.TextResourceContents(
                    uri=uri, mime_type="application/json", text=json.dumps(result["prospect"], default=str))])
        raise ValueError(f"Not found: {uri}")

    return Server(
        "agency-os",
        title="agency-os",
        instructions=("agency-os is an outreach CRM. Use search_prospects and get_prospect to look things up, "
                      "draft with the sales-persona prompts, and only change records (notes, calls, stages) "
                      "when the user asks."),
        on_list_tools=list_tools, on_call_tool=call_tool,
        on_list_prompts=list_prompts, on_get_prompt=get_prompt,
        on_list_resources=list_resources, on_list_resource_templates=list_templates, on_read_resource=read_resource,
    )


def build_app(get_db: Callable, base_url: str):
    """The Starlette app serving /mcp, OAuth (/authorize, /token, /register, /revoke) and their metadata."""
    provider = AgencyOAuthProvider(get_db, consent_url=f"{base_url}/oauth/consent")
    host = urlsplit(base_url).netloc
    auth = AuthSettings(
        issuer_url=base_url, resource_server_url=f"{base_url}/mcp", required_scopes=[READ],
        client_registration_options=ClientRegistrationOptions(enabled=True, valid_scopes=SCOPES, default_scopes=[READ]),
        revocation_options=RevocationOptions(enabled=True),
        validate_token_resource=False,  # personal tokens aren't bound to a resource; we check the user per call
    )
    security = TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=[host, "127.0.0.1:*", "localhost:*"],
        allowed_origins=[base_url, "http://127.0.0.1:*", "http://localhost:*"],
    )
    return build_server(get_db).streamable_http_app(
        streamable_http_path="/mcp", json_response=True, stateless_http=True, transport_security=security,
        auth=auth, token_verifier=ProviderTokenVerifier(provider), auth_server_provider=provider,
    )


class MCPMount:
    """ASGI app mounted at the end of the web app's routes.

    The MCP session manager needs a running task group, which a mounted app's
    own lifespan never gets, so the web app's lifespan calls running(), which
    builds a fresh app each time (a session manager can only run once).
    """

    def __init__(self, get_db: Callable, base_url: Callable[[], str]):
        self.get_db, self.base_url = get_db, base_url
        self.app = None

    @asynccontextmanager
    async def running(self):
        app = build_app(self.get_db, self.base_url())
        async with app.router.lifespan_context(app):
            self.app = app
            try:
                yield
            finally:
                self.app = None

    async def __call__(self, scope, receive, send):
        if self.app is None:
            if scope["type"] == "http":
                await JSONResponse({"detail": "Not Found"}, status_code=404)(scope, receive, send)
            return
        await self.app(scope, receive, send)
