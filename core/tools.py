"""
The tool layer: one registry of what can be done in agency-os on a user's
behalf, used by the built-in agent panel, the user's own assistant over
WebMCP (/api/tools, web/static/webmcp.js) and MCP, and the command console
(core/console.py).

Each tool names the permission it needs (a catalog permission, or the
@owner / @super_admin sentinels), the surfaces it appears on ("ai",
"console"; team administration is console-only), and its kind:

- read:  looks things up
- draft: produces text with the app's model; changes nothing
- write: changes data; needs `confirmed` (the user approved the exact change
         in the page) and is audited with where the call came from

run_tool() validates arguments against the tool's JSON Schema, checks the
user's permission, and never raises.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable

from core import access, agents, llm, verify
from core.access import AccessError, CurrentUser
from core.campaign import hidden_campaigns
from core.contact_depth import CALL_OUTCOMES
from core.models import CallLog

# Campaign configs come from the web app (it syncs campaign files from the
# database); it sets this at import. Tests may replace it.
campaign_source: Callable[[], list] = lambda: []
# This server's public URL, for invite links; the web app sets it at import.
site_url: Callable[[], str] = lambda: ""

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
    surfaces: tuple[str, ...] = ("ai", "console")

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
    elif kind == "boolean":
        if not isinstance(value, bool):
            raise ToolError(f"{path} must be true or false")
    elif kind == "array":
        if not isinstance(value, list) or len(value) > schema.get("maxItems", 50):
            raise ToolError(f"{path} must be a list of at most {schema.get('maxItems', 50)}")
        return [_check(v, schema.get("items", {}), f"{path}[{i}]") for i, v in enumerate(value)]
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


# ── Team administration (console only) ─────────────────────────────────
# The database enforces who may grant Owner/Super Admin (Database._guard_protected);
# these tools only add the permission gate and the invite email.


def _role_ids(db, names: list[str]) -> list[int]:
    by_name = {r["name"].lower(): r["id"] for r in db.list_roles()}
    unknown = [n for n in names if n.lower() not in by_name]
    if unknown:
        raise ToolError(f"Unknown role(s): {', '.join(unknown)}. "
                        f"Choose from: {', '.join(r['name'] for r in db.list_roles())}")
    return [by_name[n.lower()] for n in names]


def _users_list(db, user, args):
    names = {r["id"]: r["name"] for r in db.list_roles()}
    return {"ok": True, "users": [
        {"email": u["email"], "name": u["name"], "roles": ", ".join(sorted(names[r] for r in u["role_ids"])),
         "active": bool(u["is_active"])}
        for u in db.list_users()]}


def _invite(db, user, email: str, name: str, role_names: list[str], send: bool) -> dict:
    """Create a user with a throwaway password and give them a one-time set-password link."""
    from core import welcome

    url = site_url() or welcome.base_url()
    if not url:
        raise ToolError("This server doesn't know its public URL. Set AGENCY_OS_BASE_URL.")
    if db.get_user_by_email(email.strip().lower()):
        raise ToolError(f"A user with email {email.strip().lower()} already exists.")
    try:
        user_id = db.create_user(email, name or email.split("@")[0], access.new_session_token(),
                                 _role_ids(db, role_names), actor=user)
        link, expires_at = welcome.issue_invite(db, user_id, url, actor=user)
    except AccessError as exc:
        raise ToolError(str(exc)) from exc
    invited = db.load_current_user(user_id)
    result = {"ok": True, "email": invited.email, "roles": ", ".join(invited.roles) or "(none)"}
    if send and welcome.smtp_configured():
        subject, body = welcome.compose(invited, link, expires_at, url)
        if welcome.send(invited.email, subject, body).status == "sent":
            return {**result, "sent": f"Welcome email sent (link expires {expires_at:%b %d, %Y})"}
    # Not sent (asked not to, or no SMTP): hand the one-time link to the admin to share.
    return {**result, "link": link, "expires": f"{expires_at:%b %d, %Y}"}


def _users_invite(db, user, args):
    return _invite(db, user, args["email"], args.get("name", ""), args.get("role", []), not args.get("no_send"))


def _users_create_owner(db, user, args):
    # No password on the command line: it would sit in history and the audit log.
    return _invite(db, user, args["email"], args.get("name", ""), [access.OWNER_ROLE], not args.get("no_send"))


def _users_set_roles(db, user, args):
    row = db.get_user_by_email(args["email"].strip().lower())
    if row is None:
        raise ToolError(f"No user with email {args['email']}.")
    try:
        db.update_user(row["id"], name=row["name"], is_active=bool(row["is_active"]),
                       role_ids=_role_ids(db, args.get("role", [])), actor=user)
    except AccessError as exc:
        raise ToolError(str(exc)) from exc
    return {"ok": True, "email": row["email"], "roles": ", ".join(args.get("role", [])) or "(none)"}


def _workflows_list(db, user, args):
    from core import workflows

    return {"ok": True, "workflows": [
        {"name": w["slug"], "kind": "tutorial" if w["source"] == "tutorial" else "yours", "steps": len(w["steps"]),
         "about": w.get("description", "")}
        for w in workflows.tutorials(user) + workflows.mine(db, user)]}


def _workflows_play(db, user, args):
    from core import workflows

    key = workflows.slugify(args["name"])
    found = workflows.get_mine(db, user, key) or workflows.tutorial(user, key)
    if found is None:
        raise ToolError(f"No workflow named {args['name']}")
    return {"ok": True, "playing": found["name"], "play": {k: found[k] for k in ("name", "steps")}}


def _workflows_export(db, user, args):
    from core import workflows

    return {"ok": True, "backup": json.dumps(workflows.export(db, user), indent=2) + "\n"}


def _campaign_named(name: str, user: CurrentUser):
    """A campaign this user can see, by its full name or by words that match exactly one (e.g. "voter-guide-cbo")."""
    slug = name.strip().lower().replace(" ", "-").replace("_", "-")
    campaigns = [c for c in campaign_source() if user.sees_campaign(c)]
    exact = [c for c in campaigns if c.db_name == slug or c.name.lower() == name.strip().lower()]
    words = [w for w in slug.split("-") if w]
    found = exact or [c for c in campaigns if words and all(w in c.db_name.split("-") for w in words)]
    if len(found) != 1:
        names = ", ".join(c.db_name for c in campaigns)
        raise ToolError(f"{'Several campaigns match' if found else 'No campaign matches'} {name!r}. Campaigns: {names}")
    return found[0]


def _member_target(db, args) -> dict:
    if bool(args.get("user")) == bool(args.get("role")):
        raise ToolError("Give --user <email> or --role <role name> (one of them)")
    if args.get("user"):
        row = db.get_user_by_email(args["user"].strip().lower())
        if row is None:
            raise ToolError(f"No user with email {args['user']}.")
        return {"user_id": row["id"], "label": row["email"]}
    role = next((r for r in db.list_roles() if r["name"].lower() == args["role"].strip().lower()), None)
    if role is None:
        raise ToolError(f"Unknown role {args['role']}. Choose from: {', '.join(r['name'] for r in db.list_roles())}")
    return {"role_id": role["id"], "label": f"everyone with {role['name']}"}


def _campaigns_members(db, user, args):
    campaign = _campaign_named(args["campaign"], user)
    members = db.campaign_members(campaign.db_name)
    return {"ok": True, "campaign": campaign.db_name,
            "access": "members only (and Owners)" if members else "everyone whose role allows it",
            "members": [{"member": m["name"], "kind": m["kind"], "email": m["email"] or ""} for m in members]}


def _campaigns_assign(db, user, args):
    campaign = _campaign_named(args["campaign"], user)
    target = _member_target(db, args)
    label = target.pop("label")
    try:
        added = db.add_campaign_member(campaign.db_name, user, **target)
    except AccessError as exc:
        raise ToolError(str(exc)) from exc
    return {"ok": True, "campaign": campaign.db_name,
            "result": f"Added {label}. Only members (and Owners) see this campaign now."
            if added else f"{label} was already on it."}


def _campaigns_unassign(db, user, args):
    campaign = _campaign_named(args["campaign"], user)
    target = _member_target(db, args)
    label = target.pop("label")
    member = next((m for m in db.campaign_members(campaign.db_name)
                   if m["user_id"] == target.get("user_id") and m["role_id"] == target.get("role_id")), None)
    if member is None or not db.remove_campaign_member(campaign.db_name, member["id"], user):
        raise ToolError(f"{label} isn't a member of {campaign.db_name}.")
    left = db.campaign_members(campaign.db_name)
    return {"ok": True, "campaign": campaign.db_name, "result": f"Removed {label}." + (
        "" if left else " No members left: everyone whose role allows it sees this campaign again.")}


def _campaigns_owners(db, user, args):
    campaign = _campaign_named(args["campaign"], user)
    owners = db.campaign_owners(campaign.db_name)
    return {"ok": True, "campaign": campaign.db_name,
            "access": "assigned Owners only (and Super Admins)" if owners else "every Owner",
            "owners": [{"owner": o["name"], "email": o["email"]} for o in owners]}


def _owner_change(db, user, args, add: bool):
    campaign = _campaign_named(args["campaign"], user)
    row = db.get_user_by_email(args["user"].strip().lower())
    if row is None:
        raise ToolError(f"No user with email {args['user']}.")
    try:
        if add:
            changed = db.add_campaign_owner(campaign.db_name, row["id"], user)
            result = (f"Assigned {row['email']}. Other Owners no longer see this campaign." if changed
                      else f"{row['email']} was already assigned.")
        else:
            match = next((o for o in db.campaign_owners(campaign.db_name) if o["user_id"] == row["id"]), None)
            if match is None or not db.remove_campaign_owner(campaign.db_name, match["id"], user):
                raise ToolError(f"{row['email']} isn't assigned to {campaign.db_name}.")
            result = f"Removed {row['email']}." + ("" if db.campaign_owners(campaign.db_name)
                                                    else " No Owners assigned: every Owner sees it again.")
    except AccessError as exc:
        raise ToolError(str(exc)) from exc
    return {"ok": True, "campaign": campaign.db_name, "result": result}


EMAIL = {"type": "string", "maxLength": 254, "description": "The user's email"}
NAME = {"type": "string", "maxLength": 200, "description": "Display name (defaults to the email's local part)"}
ROLES = {"type": "array", "items": {"type": "string", "maxLength": 100}, "maxItems": 20,
         "description": "Role name; repeat for several"}
NO_SEND = {"type": "boolean", "description": "Don't email it; show the one-time link to share yourself"}
CONSOLE = ("console",)
CAMPAIGN_NAME = {"type": "string", "maxLength": 200,
                 "description": "Campaign name, or words matching one (e.g. voter-guide-cbo)"}

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
    Tool("users_list", "List team members, their roles and whether they're active.",
         _obj({}), access.OWNER, "read", _users_list, CONSOLE),
    Tool("users_invite", "Add a team member and send them a one-time set-password link. "
         "Only a Super Admin can invite with the Owner or Super Admin role.",
         _obj({"email": EMAIL, "name": NAME, "role": ROLES, "no_send": NO_SEND}, ["email"]),
         access.OWNER, "write", _users_invite, CONSOLE),
    Tool("users_create_owner", "Add a new Owner and send them a one-time set-password link.",
         _obj({"email": EMAIL, "name": NAME, "no_send": NO_SEND}, ["email"]),
         access.SUPER_ADMIN, "write", _users_create_owner, CONSOLE),
    Tool("users_set_roles", "Replace a team member's roles (none removes them all). "
         "Only a Super Admin can grant or remove Owner or Super Admin.",
         _obj({"email": EMAIL, "role": ROLES}, ["email"]), access.OWNER, "write", _users_set_roles, CONSOLE),
    Tool("campaigns_members", "Who works a campaign. A campaign with members is visible only to them (and Owners).",
         _obj({"campaign": CAMPAIGN_NAME}, ["campaign"]), access.OWNER, "read", _campaigns_members, CONSOLE),
    Tool("campaigns_assign", "Add a person (--user) or everyone with a role (--role) to a campaign. "
         "Once it has members, only they (and Owners) see it and its leads.",
         _obj({"campaign": CAMPAIGN_NAME, "user": {**EMAIL, "description": "A person's email"},
               "role": {"type": "string", "maxLength": 100, "description": "A role name, e.g. Caller"}}, ["campaign"]),
         access.OWNER, "write", _campaigns_assign, CONSOLE),
    Tool("campaigns_unassign", "Remove a person or role from a campaign. With no members left, "
         "everyone whose role allows it sees the campaign again.",
         _obj({"campaign": CAMPAIGN_NAME, "user": {**EMAIL, "description": "A person's email"},
               "role": {"type": "string", "maxLength": 100, "description": "A role name, e.g. Caller"}}, ["campaign"]),
         access.OWNER, "write", _campaigns_unassign, CONSOLE),
    Tool("campaigns_owners", "Which Owners run a campaign (assigned by a Super Admin; others don't see it).",
         _obj({"campaign": CAMPAIGN_NAME}, ["campaign"]), access.OWNER, "read", _campaigns_owners, CONSOLE),
    Tool("campaigns_add_owner", "Assign an Owner to a campaign. Once it has any, other Owners don't see it.",
         _obj({"campaign": CAMPAIGN_NAME, "user": {**EMAIL, "description": "The Owner's email"}}, ["campaign", "user"]),
         access.SUPER_ADMIN, "write", lambda db, user, args: _owner_change(db, user, args, True), CONSOLE),
    Tool("campaigns_remove_owner", "Unassign an Owner from a campaign. With none left, every Owner sees it again.",
         _obj({"campaign": CAMPAIGN_NAME, "user": {**EMAIL, "description": "The Owner's email"}}, ["campaign", "user"]),
         access.SUPER_ADMIN, "write", lambda db, user, args: _owner_change(db, user, args, False), CONSOLE),
    Tool("workflows_list", "Tutorials and your own workflows (play one with workflows play --name ...).",
         _obj({}), access.ANY_USER, "read", _workflows_list, CONSOLE),
    Tool("workflows_play", "Play a tutorial or workflow in your browser: it moves around the app and explains each step.",
         _obj({"name": {"type": "string", "maxLength": 120, "description": "Its name, from workflows list"}}, ["name"]),
         access.ANY_USER, "read", _workflows_play, CONSOLE),
    Tool("workflows_export", "Back up all your workflows as JSON (e.g. agency_os.py remote workflows export > backup.json).",
         _obj({}), access.ANY_USER, "read", _workflows_export, CONSOLE),
]}


def available(user: CurrentUser, surface: str = "ai") -> list[Tool]:
    """The tools this user may call on a surface (drafting only when a model is configured).

    On the console, drafting also needs the user's own AI opt-in, as it does in the app.
    """
    return [t for t in TOOLS.values()
            if surface in t.surfaces and user.allows(t.permission)
            and (t.kind != "draft" or (llm.backend() and (surface == "ai" or user.uses_ai(t.permission))))]


def run_tool(db, user: CurrentUser, name: str, args: Any, *, source: str, confirmed: bool = False,
             surface: str = "ai") -> dict:
    """Run one tool as `user`. Returns {ok, ...}; never raises."""
    tool = TOOLS.get(name)
    if tool is None or tool not in available(user, surface):
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
        # Team tools have no prospect; the database's own user.* entries carry the user id.
        target = ("prospect", clean["prospect_id"]) if "prospect_id" in clean else (None, None)
        db.audit(user, f"tool.{name}", *target, {"source": source, "args": clean})
    return result
