"""
The AI tool layer: one registry of what an AI may do in agency-os, used by
the built-in agent panel and by the user's own assistant over WebMCP
(/api/tools, web/static/webmcp.js).

Each tool names the catalog permission it needs and its kind:

- read:  looks things up
- draft: produces text with the app's model; changes nothing
- write: changes data; needs `confirmed` (the user approved the exact change
         in the page) and is audited with where the call came from

run_tool() validates arguments against the tool's JSON Schema, checks the
user's permission, and never raises.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable

from core import agents, llm, verify
from core.access import CurrentUser
from core.campaign import hidden_campaigns
from core.contact_depth import CALL_OUTCOMES
from core.models import CallLog

# Campaign configs come from the web app (it syncs campaign files from the
# database); it sets this at import. Tests may replace it.
campaign_source: Callable[[], list] = lambda: []

STAGES = ["cold", "contacted", "engaged", "demo_scheduled", "proposal_sent", "closed_won", "closed_lost", "nurture"]
MAX_NOTE = 4000


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    input_schema: dict
    permission: str
    kind: str  # read | draft | write
    handler: Callable[[Any, CurrentUser, dict], dict]

    def public(self) -> dict:
        return {"name": self.name, "description": self.description, "inputSchema": self.input_schema,
                "kind": self.kind}


class ToolError(ValueError):
    """A tool refused its input; the message is safe to show the user."""


# ── Argument validation (the subset of JSON Schema our tools use) ──────


def _check(value: Any, schema: dict, path: str) -> Any:
    kind = schema.get("type")
    if kind == "integer":
        if isinstance(value, bool) or not isinstance(value, int):
            if isinstance(value, str) and value.strip().lstrip("-").isdigit():
                value = int(value)
            else:
                raise ToolError(f"{path} must be a whole number")
        if "minimum" in schema and value < schema["minimum"]:
            raise ToolError(f"{path} must be at least {schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            raise ToolError(f"{path} must be at most {schema['maximum']}")
    elif kind == "string":
        if not isinstance(value, str):
            raise ToolError(f"{path} must be text")
        if len(value) > schema.get("maxLength", 10_000):
            raise ToolError(f"{path} is too long")
    if "enum" in schema and value not in schema["enum"]:
        raise ToolError(f"{path} must be one of: {', '.join(map(str, schema['enum']))}")
    return value


def validate(schema: dict, args: Any) -> dict:
    if not isinstance(args, dict):
        raise ToolError("Arguments must be an object")
    props = schema.get("properties", {})
    unknown = set(args) - set(props)
    if unknown:
        raise ToolError(f"Unknown argument: {sorted(unknown)[0]}")
    for name in schema.get("required", []):
        if args.get(name) in (None, ""):
            raise ToolError(f"{name} is required")
    return {k: _check(v, props[k], k) for k, v in args.items() if v is not None}


def _obj(properties: dict, required: list[str] = ()) -> dict:
    return {"type": "object", "properties": properties, "required": list(required), "additionalProperties": False}


PROSPECT_ID = {"type": "integer", "minimum": 1, "description": "The prospect's id"}

# ── Context shared by tools and the agent panel ───────────────────────


def _hidden(user: CurrentUser) -> list[str]:
    return hidden_campaigns(campaign_source(), user)


def _require_visible(db, user: CurrentUser, prospect_id: int) -> None:
    """Prospects in campaigns this user may not see (e.g. recruiting) don't exist for their AI either."""
    if db.prospect_hidden(prospect_id, _hidden(user)):
        raise ToolError("Prospect not found")


def _outreach(db, prospect_id: int, campaign: str = "") -> dict:
    sql = """SELECT o.*, c.name AS campaign_name FROM outreach o JOIN campaigns c ON c.id = o.campaign_id
             WHERE o.prospect_id = ?"""
    params: tuple = (prospect_id,)
    if campaign:
        sql += " AND c.name = ?"
        params += (campaign,)
    row = db.conn.execute(sql + " ORDER BY o.updated_at DESC LIMIT 1", params).fetchone()
    if row is None:
        raise ToolError("That prospect isn't in a campaign" + (f" named {campaign}" if campaign else ""))
    return dict(row)


def build_prospect_context(db, user: CurrentUser, prospect_id: int) -> dict:
    """What an AI may know about a prospect: their record, campaigns, calls and email subjects."""
    _require_visible(db, user, prospect_id)
    prospect = db.get_prospect(prospect_id)
    if prospect is None:
        raise ToolError("Prospect not found")
    outreach = [dict(r) for r in db.conn.execute(
        """SELECT o.id, o.stage, o.touch_count, o.contact_name, o.contact_title, o.contact_email, o.contact_phone,
                  o.last_contacted_at, o.next_follow_up_at, o.notes, c.name AS campaign
           FROM outreach o JOIN campaigns c ON c.id = o.campaign_id WHERE o.prospect_id = ?""",
        (prospect_id,)).fetchall()]
    context: dict = {
        "id": prospect.id, "name": prospect.name, "city": prospect.city, "state": prospect.state,
        "website": prospect.website_url, "focus_area": prospect.focus_area, "ntee_code": prospect.ntee_code,
        "annual_revenue": prospect.annual_revenue, "campaigns": outreach,
    }
    if user.can("calls.view"):
        context["recent_calls"] = [
            {k: c[k] for k in ("called_at", "outcome", "interest_level", "decision_maker_name",
                               "decision_maker_role", "next_step", "notes")}
            for c in db.get_calls_for_prospect(prospect_id)[:10]]
    if user.can("emails.view"):
        context["recent_emails"] = [dict(r) for r in db.conn.execute(
            """SELECT e.subject, e.status, e.sent_at FROM email_log e JOIN outreach o ON o.id = e.outreach_id
               WHERE o.prospect_id = ? ORDER BY e.sent_at DESC LIMIT 10""", (prospect_id,)).fetchall()]
    check = verify.lead_verdict(db, prospect_id)
    if check:
        context["lead_package_check"] = {"status": check["status"],
                                         "evidence": [s["label"] for s in check["signals"]]}
    return context


# ── Handlers ───────────────────────────────────────────────────────────


def _search_prospects(db, user, args):
    hidden_sql, params = db.hidden_clause(_hidden(user))
    where = [hidden_sql]
    if args.get("q"):
        where.append("(p.name ILIKE ? OR p.city ILIKE ? OR p.ein ILIKE ? OR p.focus_area ILIKE ?)")
        params += [f"%{args['q']}%"] * 4
    if args.get("stage"):
        where.append("o.stage = ?")
        params.append(args["stage"])
    if args.get("campaign"):
        where.append("c.name = ?")
        params.append(args["campaign"])
    sql = """SELECT p.id, p.name, p.city, p.state, o.stage, c.name AS campaign FROM prospects p
             LEFT JOIN outreach o ON o.prospect_id = p.id LEFT JOIN campaigns c ON c.id = o.campaign_id"""
    if where:
        sql += " WHERE " + " AND ".join(where)
    rows = db.conn.execute(sql + " ORDER BY p.name LIMIT ?", (*params, args.get("limit", 20))).fetchall()
    return {"ok": True, "prospects": [dict(r) for r in rows]}


def _get_prospect(db, user, args):
    return {"ok": True, "prospect": build_prospect_context(db, user, args["prospect_id"])}


def _list_calls(db, user, args):
    calls = db.get_calls_for_prospect(args["prospect_id"])[: args.get("limit", 20)]
    keys = ("id", "called_at", "outcome", "interest_level", "decision_maker_name", "next_step", "notes", "called_by")
    return {"ok": True, "calls": [{k: c[k] for k in keys} for c in calls]}


def _get_campaign(db, user, args):
    campaign = next((c for c in campaign_source() if c.db_name == args["campaign"] and user.sees_campaign(c)),
                    None)
    if campaign is None:
        raise ToolError("Campaign not found")
    return {"ok": True, "campaign": {
        "name": campaign.db_name, "title": campaign.name, "product": campaign.product,
        "channels": campaign.channels, "stages": campaign.stages or STAGES,
        "cadence": [{"touch": s.touch, "delay_days": s.delay_days, "script": s.script, "next_stage": s.next_stage}
                    for s in campaign.cadence],
    }}


def _list_agents(db, user, args):
    return {"ok": True, "agents": [
        {"key": p.key, "name": p.name, "description": p.description,
         "tasks": [{"key": t, "label": agents.TASKS[t][0]} for t in p.tasks]}
        for p in agents.load_personas().values()]}


def _get_agent(db, user, args):
    persona = agents.load_personas().get(args["agent"])
    if persona is None:
        raise ToolError("Unknown agent")
    return {"ok": True, "agent": {"key": persona.key, "name": persona.name, "persona": persona.body}}


def _draft(db, user, args):
    context = build_prospect_context(db, user, args["prospect_id"])
    return agents.draft(args["agent"], args["task"], context, args.get("instructions", ""))


def _add_note(db, user, args):
    row = _outreach(db, args["prospect_id"], args.get("campaign", ""))
    stamp = f"[{datetime.now():%Y-%m-%d} {user.name}]"
    notes = f"{row['notes']}\n\n" if row["notes"] else ""
    db.update_outreach(row["id"], {"notes": f"{notes}{stamp} {args['note'].strip()}"[-20_000:]})
    return {"ok": True, "outreach_id": row["id"]}


def _log_call(db, user, args):
    row = _outreach(db, args["prospect_id"], args.get("campaign", ""))
    call_id = db.log_call(CallLog(
        outreach_id=row["id"], campaign_id=row["campaign_id"], prospect_id=args["prospect_id"],
        outcome=args["outcome"], notes=args.get("notes") or None,
        decision_maker_name=args.get("decision_maker_name") or None,
        stage_at_call=row["stage"], called_by=user.name, called_by_user_id=user.id,
    ))
    return {"ok": True, "call_id": call_id}


def _set_stage(db, user, args):
    row = _outreach(db, args["prospect_id"], args.get("campaign", ""))
    updates = {"stage": args["stage"]}
    if args["stage"] in ("closed_won", "closed_lost"):
        updates["closed_at"] = datetime.now().isoformat()
    db.update_outreach(row["id"], updates)
    return {"ok": True, "outreach_id": row["id"], "from": row["stage"], "to": args["stage"]}


CAMPAIGN = {"type": "string", "maxLength": 200, "description": "Campaign name; defaults to the most recent"}

TOOLS: dict[str, Tool] = {t.name: t for t in [
    Tool("search_prospects", "Search prospects by name, city, EIN or focus area, optionally by stage or campaign.",
         _obj({"q": {"type": "string", "maxLength": 200}, "stage": {"type": "string", "enum": STAGES},
               "campaign": {"type": "string", "maxLength": 200},
               "limit": {"type": "integer", "minimum": 1, "maximum": 50}}),
         "prospects.view", "read", _search_prospects),
    Tool("get_prospect", "Everything known about one prospect: record, campaigns, contacts, recent calls and emails.",
         _obj({"prospect_id": PROSPECT_ID}, ["prospect_id"]), "prospects.view", "read", _get_prospect),
    Tool("list_calls", "A prospect's logged calls, newest first.",
         _obj({"prospect_id": PROSPECT_ID, "limit": {"type": "integer", "minimum": 1, "maximum": 100}},
              ["prospect_id"]), "calls.view", "read", _list_calls),
    Tool("get_campaign", "A campaign's product, channels, stages and email cadence.",
         _obj({"campaign": {"type": "string", "maxLength": 200}}, ["campaign"]), "campaigns.view", "read",
         _get_campaign),
    Tool("list_agent_personas", "The sales personas available for drafting, and the tasks each one does.",
         _obj({}), "agents.use", "read", _list_agents),
    Tool("get_agent_persona", "A persona's full instructions, to use as a system prompt in your own AI.",
         _obj({"agent": {"type": "string", "maxLength": 64}}, ["agent"]), "agents.use", "read", _get_agent),
    Tool("draft_with_agent", "Draft with a sales persona and agency-os's own AI model (email, call prep, deal "
         "review...). Returns text only; nothing is sent.",
         _obj({"prospect_id": PROSPECT_ID, "agent": {"type": "string", "maxLength": 64},
               "task": {"type": "string", "enum": list(agents.TASKS)},
               "instructions": {"type": "string", "maxLength": agents.MAX_INSTRUCTIONS}},
              ["prospect_id", "agent", "task"]), "agents.use", "draft", _draft),
    Tool("add_prospect_note", "Add a dated note to a prospect's campaign record.",
         _obj({"prospect_id": PROSPECT_ID, "note": {"type": "string", "maxLength": MAX_NOTE}, "campaign": CAMPAIGN},
              ["prospect_id", "note"]), "prospects.edit", "write", _add_note),
    Tool("log_call", "Record a call with a prospect.",
         _obj({"prospect_id": PROSPECT_ID, "outcome": {"type": "string", "enum": list(CALL_OUTCOMES)},
               "notes": {"type": "string", "maxLength": MAX_NOTE},
               "decision_maker_name": {"type": "string", "maxLength": 200}, "campaign": CAMPAIGN},
              ["prospect_id", "outcome"]), "calls.log", "write", _log_call),
    Tool("set_stage", "Move a prospect to another pipeline stage.",
         _obj({"prospect_id": PROSPECT_ID, "stage": {"type": "string", "enum": STAGES}, "campaign": CAMPAIGN},
              ["prospect_id", "stage"]), "pipeline.edit", "write", _set_stage),
]}


def available(user: CurrentUser) -> list[Tool]:
    """The tools this user may call (drafting only when a model is configured)."""
    return [t for t in TOOLS.values()
            if user.can(t.permission) and (t.kind != "draft" or llm.backend())]


def run_tool(db, user: CurrentUser, name: str, args: Any, *, source: str, confirmed: bool = False) -> dict:
    """Run one tool as `user`. Returns {ok, ...}; never raises."""
    tool = TOOLS.get(name)
    if tool is None or tool not in available(user):
        return {"ok": False, "error": f"No tool named {name} for you"}
    if tool.kind == "write" and not confirmed:
        return {"ok": False, "error": "This change needs your confirmation", "needs_confirmation": True}
    try:
        clean = validate(tool.input_schema, args or {})
        if "prospect_id" in clean:
            _require_visible(db, user, clean["prospect_id"])
        result = tool.handler(db, user, clean)
    except ToolError as exc:
        return {"ok": False, "error": str(exc)}
    except Exception as exc:  # a tool bug must not leak a traceback to an AI client
        return {"ok": False, "error": f"{name} failed: {type(exc).__name__}"}
    if tool.kind == "write" and result.get("ok"):
        db.audit(user, f"tool.{name}", "prospect", clean.get("prospect_id"), {"source": source, "args": clean})
    return result
