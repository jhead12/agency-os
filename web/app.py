"""
FastAPI web dashboard for agency-os.

Run:
    source .env
    python3 -m web.app

Then open http://localhost:8000

Features:
    - Dashboard with pipeline stats across all campaigns
    - Prospect list with search, filter by source, stage
    - Prospect detail with outreach timeline and email log
    - Stage management (move prospects between stages)
    - Email log viewer
    - Campaign overview
    - Multi-user sign-in with role-based permissions (see core/access.py)
"""

from __future__ import annotations

import asyncio
import json
import os
import secrets
import sys
import tempfile
import time
from collections import defaultdict
from contextlib import asynccontextmanager
from typing import Optional
from pathlib import Path
from datetime import datetime, timezone
from urllib.parse import quote, urlsplit, urlencode

import yaml

# Ensure project root is on path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from core.env import load_dotenv  # noqa: E402

load_dotenv()  # .env settings for local runs; a deploy's own environment always wins

from fastapi import BackgroundTasks, FastAPI, Request, Query, HTTPException, Form, Depends, UploadFile, File
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from jinja2 import ChoiceLoader, FileSystemLoader, PrefixLoader

from core import access
from core.access import AccessError, CurrentUser
from core.db import Database
from core.campaign import discover_campaigns, hidden_campaigns, sync_campaign_files
from core.registry import PluginRegistry
from core.pipeline import Pipeline
from core.protocols import portal_product
from plugins.channels.lob_direct_mail import TEMPLATE_ID_RE, lob_template_url
from core.jobs import JobRunner, configured_jobs, jobs_enabled
from core import (
    accounts, agents, claims, compliance, console, contact_depth, evidence, lead_packages, llm, mcp_auth, panels, payments,
    generator, plugin_pages, plugin_panels, royalties, searches, selling,
    tools, verify, voice, workflows,
)
from core import welcome as welcome_email
from core.welcome import base_url as public_base_url
from web.mcp_server import MCPMount
from starlette.concurrency import run_in_threadpool

# ── Init ────────────────────────────────────────────────────────────

# All data lives in PostgreSQL (DATABASE_URL), including the editable campaign
# files. Those are written to CAMPAIGNS_DIR, a local cache, so they can be loaded
# from disk; every edit is saved to the database too (save_campaign_file).
DB_URL = os.environ.get("DATABASE_URL", "")
CAMPAIGNS_DIR = Path(os.environ.get(
    "AGENCY_OS_CAMPAIGNS_DIR", str(Path(tempfile.gettempdir()) / "agency-os-campaigns")))
_campaigns_synced = False


def get_db() -> Database:
    return Database(DB_URL or None)


def get_campaigns():
    global _campaigns_synced
    if not _campaigns_synced:
        sync_campaign_files(get_db(), PROJECT_ROOT / "campaigns", CAMPAIGNS_DIR)
        _campaigns_synced = True
    return discover_campaigns(str(CAMPAIGNS_DIR))


tools.campaign_source = lambda: get_campaigns()  # the AI tools read campaigns through the web app's cache
tools.site_url = lambda: site_url()  # invite links from the console's user tools
selling.campaign_source = lambda: get_campaigns()  # packages only draw from campaigns their publisher sees


def visible_campaigns(request: Request) -> list:
    """The campaigns this user may see (campaign.yaml `requires_permission`, e.g. recruiting.view)."""
    user = current_user(request)
    return [c for c in get_campaigns() if user.sees_campaign(c)]


def hidden_for(request: Request) -> list[str]:
    """Campaign names whose prospects this user may not see; pass to the Database list queries."""
    return hidden_campaigns(get_campaigns(), current_user(request))


def require_visible_prospect(request: Request, prospect_id: int) -> None:
    """404 for a prospect in a campaign this user may not see, as if it didn't exist."""
    if prospect_id and get_db().prospect_hidden(prospect_id, hidden_for(request)):
        raise HTTPException(status_code=404, detail="Prospect not found")


def _invalidate_campaign_cache():
    """Force get_campaigns() to re-sync from the database on next call."""
    global _campaigns_synced
    _campaigns_synced = False


def save_campaign_file(path: Path, content: str) -> None:
    """Write a campaign file to the local cache and to the database."""
    path.write_text(content)
    get_db().save_campaign_file(path.resolve().relative_to(CAMPAIGNS_DIR.resolve()).as_posix(), content)


def get_job_runner() -> JobRunner:
    return JobRunner(get_db().url, str(PROJECT_ROOT / "plugins"), get_campaigns)


def site_url() -> str:
    """This server's public URL: AGENCY_OS_BASE_URL, Railway's domain, or the local port."""
    return public_base_url() or f"http://127.0.0.1:{os.environ.get('PORT', '8000')}"


mcp_mount = MCPMount(lambda: get_db(), site_url)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    bootstrap_access()
    # Background jobs (core/jobs.py): on for the deployed service only.
    task = asyncio.create_task(get_job_runner().loop()) if jobs_enabled() else None
    # Paid account searches (core/searches.py): checked every few seconds, not on the job schedule.
    search_task = asyncio.create_task(searches.SearchRunner(DB_URL).loop()) if searches.runs_enabled() else None
    async with mcp_mount.running():  # the MCP server (/mcp) and its OAuth endpoints
        yield
    for running in (task, search_task):
        if running:
            running.cancel()


app = FastAPI(title="agency-os", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)

templates = Jinja2Templates(directory=str(PROJECT_ROOT / "web" / "templates"))
app.mount("/static", StaticFiles(directory=str(PROJECT_ROOT / "web" / "static")), name="static")


# Sent on every response. No page is ever framed; links never pass the URL
# (a one-time /welcome/ or /reset-password/ token) to another site.
SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "same-origin",
    "Permissions-Policy": "geolocation=(), camera=(), payment=()",
}


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    for name, value in SECURITY_HEADERS.items():
        response.headers.setdefault(name, value)
    if request.url.scheme == "https":
        response.headers.setdefault("Strict-Transport-Security", "max-age=31536000")
    if request.url.path.startswith(("/welcome/", "/reset-password/")):
        response.headers["Cache-Control"] = "no-store"
    return response
# Plugin pages (core/plugin_pages.py) and panels (core/plugin_panels.py): their
# templates load as "plugin/<name>" and "plugin-panel/<name>", after the core
# ones, so a plugin can extend base.html but never replace a core page.
templates.env.loader = ChoiceLoader([templates.env.loader, PrefixLoader({
    plugin_pages.TEMPLATE_PREFIX: FileSystemLoader(str(plugin_pages.TEMPLATES_DIR)),
    plugin_panels.TEMPLATE_PREFIX: FileSystemLoader(str(plugin_panels.TEMPLATES_DIR)),
})])
app.mount("/plugin-static", StaticFiles(directory=str(plugin_pages.STATIC_DIR), check_dir=False),
          name="plugin-static")

# ── Auth ────────────────────────────────────────────────────────────
# Multi-user login with role-based permissions (policy in core/access.py).
# Every route is checked against access.ROUTE_RULES before its handler
# runs; a route missing from that map is denied for everyone.

SESSION_COOKIE = "aos_session"
SESSION_TTL_DAYS = 14
LOGIN_WINDOW_SECONDS = 15 * 60
LOGIN_MAX_FAILURES = 5          # per email and client IP
LOGIN_ACCOUNT_MAX_FAILURES = 20  # per email from any IP, so spreading guesses over IPs doesn't help
# Routes that also accept a CLI key (Authorization: Bearer aos_cli_...) instead of a session.
KEY_ROUTES = {"POST /api/console"}
_login_failures: dict[str, list[float]] = defaultdict(list)
# Password-reset requests per email and per client IP, same window as logins
RESET_MAX_REQUESTS = 3
_reset_requests: dict[str, list[float]] = defaultdict(list)
_DUMMY_HASH = access.hash_password(secrets.token_hex(16))


class LoginRequired(Exception):
    pass


class Forbidden(Exception):
    pass


def bootstrap_access() -> None:
    """Install roles and, on a fresh DB, create the owner from env vars."""
    db = get_db()
    db.install_access()
    email = os.environ.get("AGENCY_OS_OWNER_EMAIL", "").strip()
    password = os.environ.get("AGENCY_OS_OWNER_PASSWORD", "")
    if db.count_users() == 0 and email and password:
        owner_id = next(r["id"] for r in db.list_roles() if r["name"] == access.OWNER_ROLE)
        name = os.environ.get("AGENCY_OS_OWNER_NAME", "").strip() or email.split("@")[0]
        db.create_user(email, name, password, [owner_id], actor=None)
        print(f"agency-os: created owner account {email}")
    if os.environ.get("AGENCY_OS_PASSWORD"):
        print("agency-os: AGENCY_OS_PASSWORD is no longer used — sign in with a user account")


def current_user(request: Request) -> CurrentUser:
    return request.state.user


def _same_origin(request: Request) -> bool:
    """Reject cross-site form posts (CSRF). Cookies are also SameSite=Lax."""
    source = request.headers.get("origin") or request.headers.get("referer")
    if not source:
        return True
    return urlsplit(source).netloc == request.headers.get("host")


async def authorize(request: Request) -> None:
    rule = access.rule_for(request.method, request.scope["route"].path)
    request.state.user = None
    if rule == access.PUBLIC:
        return
    db = get_db()
    scheme, _, key = request.headers.get("authorization", "").partition(" ")
    request.state.via_key = (scheme.lower() == "bearer" and bool(key)
                             and f"{request.method} {request.scope['route'].path}" in KEY_ROUTES)
    if request.state.via_key:
        user_id = mcp_auth.cli_key_user_id(db, key.strip())
    else:
        token = request.cookies.get(SESSION_COOKIE)
        user_id = db.session_user_id(access.hash_token(token)) if token else None
    user = db.load_current_user(user_id) if user_id else None
    if user is None:
        raise LoginRequired()
    request.state.user = user
    if rule is None or not user.allows(rule):
        raise Forbidden()
    # A key isn't sent by browsers on their own, so only cookie requests need the CSRF check.
    if request.method not in ("GET", "HEAD", "OPTIONS") and not request.state.via_key and not _same_origin(request):
        raise Forbidden()
    # Campaign-restricted leads (e.g. attorneys for recruiters): every route
    # taking one of these ids in its path is covered here.
    ids = {}
    for key in ("prospect_id", "lead_package_id"):
        try:
            ids[key] = int(request.path_params[key])
        except (KeyError, ValueError):
            pass  # absent, or not a number (the route itself rejects that)
    if "prospect_id" in ids:
        require_visible_prospect(request, ids["prospect_id"])
    # Campaign settings pages: a campaign this user can't see doesn't exist for them.
    if request.path_params.get("campaign_slug") in hidden_for(request):
        raise HTTPException(status_code=404, detail="Campaign not found")
    if "lead_package_id" in ids:
        package = db.get_lead_package(ids["lead_package_id"])
        if package and package["campaign_name"] in hidden_for(request):
            raise HTTPException(status_code=404, detail="Package not found")


# Applies to every route declared below.
app.router.dependencies.append(Depends(authorize))


def _wants_json(request: Request) -> bool:
    return request.url.path.startswith("/api/") or request.url.path.endswith(".ics")


@app.exception_handler(LoginRequired)
async def _login_required(request: Request, _exc: LoginRequired):
    if _wants_json(request):
        return JSONResponse({"detail": "Not authenticated"}, status_code=401)
    target = request.url.path + (f"?{request.url.query}" if request.url.query else "")
    return RedirectResponse(url=f"/login?next={quote(target)}", status_code=303)


@app.exception_handler(Forbidden)
async def _forbidden(request: Request, _exc: Forbidden):
    if _wants_json(request):
        return JSONResponse({"detail": "Forbidden"}, status_code=403)
    return templates.TemplateResponse(request, "forbidden.html", {}, status_code=403)


# ── Helpers ─────────────────────────────────────────────────────────


def fmt_currency(val) -> str:
    if not val:
        return "—"
    if val >= 1_000_000:
        return f"${val / 1_000_000:.1f}M"
    if val >= 1_000:
        return f"${val / 1_000:.0f}K"
    return f"${val}"


def tel_href(phone) -> str:
    """A tel: link for a stored phone number, or "" if it isn't dialable.

    US numbers get +1 (10 digits, or 11 starting with 1); numbers written with a
    leading + keep their country code. Opening the link hands the call to the
    device: the phone app on mobile, "Call from iPhone" on a Mac, Phone Link on Windows.
    """
    number = voice.e164(phone)
    return f"tel:{number}" if number else ""


def fmt_date(val) -> str:
    if not val:
        return "—"
    if isinstance(val, str):
        try:
            val = datetime.fromisoformat(val)
        except ValueError:
            return val
    return val.strftime("%b %d, %Y")


def days_until(val) -> int:
    """Whole days from today to a date (negative once it's past)."""
    if isinstance(val, str):
        val = datetime.fromisoformat(val)
    return (val.date() - datetime.now().date()).days


def days_ago(val) -> str:
    days = -days_until(val)
    return "today" if days <= 0 else "yesterday" if days == 1 else f"{days} days ago"


def nav_active(path: str) -> str:
    """The nav item to highlight for a URL path, e.g. "/prospects/12" -> "prospects"."""
    if path == "/":
        return "dashboard"
    parts = path.strip("/").split("/")
    if parts[0] == "p" and len(parts) > 1:
        return f"p-{parts[1]}"
    if parts[0] == "admin":
        return {"campaigns": "admin-campaigns", "selling": "admin-selling", "payouts": "admin-payouts",
                "generator": "admin-generator"}.get(
            parts[1] if len(parts) > 1 else "", "admin")
    return parts[0]


templates.env.filters["currency"] = fmt_currency
templates.env.filters["fmt_date"] = fmt_date
templates.env.filters["tel"] = tel_href
templates.env.filters["days_until"] = days_until
templates.env.filters["days_ago"] = days_ago
templates.env.globals["nav_active"] = nav_active
templates.env.globals["CALL_OUTCOMES"] = contact_depth.CALL_OUTCOMES
templates.env.globals["chat_personas"] = lambda: list(agents.load_personas().values())
templates.env.globals["ai_model"] = llm.describe
templates.env.globals["plugin_pages_for"] = plugin_pages.visible_to
templates.env.globals["super_admin_channels"] = access.SUPER_ADMIN_CHANNELS


# ── Routes ──────────────────────────────────────────────────────────


@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    """Main dashboard — pipeline overview across all campaigns."""
    db = get_db()
    campaigns = visible_campaigns(request)

    all_stats = []
    for c in campaigns:
        stats = db.get_pipeline_stats(c.db_name)
        stats["config"] = c
        all_stats.append(stats)

    # Aggregate totals
    total_prospects = sum(s["total_prospects"] for s in all_stats)
    total_emails = sum(s["total_emails_sent"] for s in all_stats)

    return templates.TemplateResponse(request, "dashboard.html", {
        "plugin_panels": await render_plugin_panels(request, "dashboard"),
        "campaigns": all_stats,
        "total_prospects": total_prospects,
        "total_emails": total_emails,
    })


SAVED_LIST_FIELDS = ("q", "source", "stage", "cities", "campaign", "generator_run", "sort", "dir", "per_page")


def prospect_list_criteria(values) -> dict:
    # Never persist pagination, print mode, arbitrary URLs, or unknown query fields.
    criteria = {key: str(values.get(key, ""))[:2000] for key in SAVED_LIST_FIELDS}
    criteria["dir"] = "desc" if criteria["dir"] == "desc" else "asc"
    try:
        criteria["per_page"] = str(max(10, min(500, int(criteria["per_page"] or 50))))
    except ValueError:
        criteria["per_page"] = "50"
    return criteria


@app.post("/prospects/saved-lists")
async def save_prospect_list(request: Request):
    form = await request.form()
    criteria = prospect_list_criteria(form)
    name = str(form.get("name", "")).strip()
    if not 1 <= len(name) <= 80:
        raise HTTPException(status_code=422, detail="List name must be between 1 and 80 characters.")
    get_db().save_prospect_list(current_user(request).id, name, criteria)
    return RedirectResponse(url="/prospects?" + urlencode(criteria), status_code=303)


@app.post("/prospects/saved-lists/{list_id}/delete")
async def delete_prospect_list(request: Request, list_id: int):
    get_db().delete_prospect_saved_list(current_user(request).id, list_id)
    return RedirectResponse(url="/prospects?" + urlencode(prospect_list_criteria(await request.form())), status_code=303)


PROSPECT_SORTS = {
    "name": "p.name",
    "city": "p.city",
    "source": "p.source",
    "focus": "p.focus_area",
    "revenue": "p.annual_revenue",
    "stage": "o.stage",
    "touch": "o.touch_count",
    "contact": "o.contact_email",
    "voter": "p.voter_engagement",
    "followup": "o.next_follow_up_at",
}


def prospect_list_order(sort: str, direction: str, sort_given: bool) -> tuple[str, str]:
    """(ORDER BY clause, "ASC"/"DESC") for the prospect list. Every column is
    sortable, nulls last; revenue defaults to biggest first."""
    sort_col = PROSPECT_SORTS.get(sort, "p.name")
    sort_dir = "DESC" if direction.lower() == "desc" else "ASC"
    if sort == "revenue" and direction == "asc" and not sort_given:
        sort_dir = "DESC"
    return f"CASE WHEN {sort_col} IS NULL THEN 1 ELSE 0 END, {sort_col} {sort_dir}", sort_dir


@app.get("/prospects", response_class=HTMLResponse)
async def prospect_list(
    request: Request,
    q: str = Query(default="", description="Search name, city, EIN, zip, focus area, website"),
    source: str = Query(default="", description="Filter by source"),
    stage: str = Query(default="", description="Filter by stage"),
    cities: str = Query(default="", description="Comma-separated city filter"),
    campaign: str = Query(default="", description="Filter by campaign slug"),
    generator_run: str = Query(default="", description="Leads one lead package generator run found"),
    sort: str = Query(default="name", description="Sort column"),
    dir: str = Query(default="asc", description="Sort direction: asc or desc"),
    page: int = Query(default=1, ge=1),
    per_page: int = Query(default=50, ge=10, le=500),
    print: int = Query(default=0, description="Print view: 1 = all rows, no pagination"),
):
    """Prospect list with search, filters, sortable columns, pagination, print view."""
    if print and not current_user(request).can("prospects.export"):
        raise Forbidden()
    db = get_db()
    campaigns = visible_campaigns(request)
    campaign_map = {c.db_name: c for c in campaigns}
    hidden = hidden_for(request)
    hidden_sql, hidden_params = db.hidden_clause(hidden)

    where_clause, params = db.prospect_filter(
        {"q": q, "source": source, "stage": stage, "campaign": campaign, "cities": cities,
         "generator_run": generator_run}, hidden)

    # Count total
    count_sql = f"""
        SELECT COUNT(*) FROM prospects p
        LEFT JOIN outreach o ON p.id = o.prospect_id
        WHERE {where_clause}
    """
    total = db.conn.execute(count_sql, params).fetchone()[0]

    order, sort_dir = prospect_list_order(sort, dir, sort in request.query_params)

    # Print mode: show all rows (up to 1000), no pagination
    if print:
        data_sql = f"""
            SELECT p.*, o.stage, o.touch_count, o.last_contacted_at,
                   o.next_follow_up_at, o.contact_name, o.contact_email,
                   o.contact_phone, o.contact_title,
                   o.id as outreach_id, o.campaign_id
            FROM prospects p
            LEFT JOIN outreach o ON p.id = o.prospect_id
            WHERE {where_clause}
            ORDER BY {order}
            LIMIT 1000
        """
        rows = db.conn.execute(data_sql, params).fetchall()

        # Get distinct cities for reference
        all_cities = [r["city"] for r in db.conn.execute(
            f"SELECT DISTINCT city FROM prospects p WHERE city IS NOT NULL AND city != '' AND {hidden_sql} "
            "ORDER BY city", hidden_params,
        ).fetchall()]

        from datetime import datetime as _dt

        return templates.TemplateResponse(request, "prospects_print.html", {
            "prospects": rows,
            "total": len(rows),
            "q": q,
            "source_filter": source,
            "stage_filter": stage,
            "cities_filter": cities,
            "sort": sort,
            "sort_dir": sort_dir.lower(),
            "all_cities": all_cities,
            "now": _dt.now().strftime("%B %d, %Y at %I:%M %p"),
        })

    offset = (page - 1) * per_page
    data_sql = f"""
        SELECT p.*, o.stage, o.touch_count, o.last_contacted_at,
               o.next_follow_up_at, o.contact_name, o.contact_email,
               o.contact_phone, o.contact_title,
               o.id as outreach_id, o.campaign_id
        FROM prospects p
        LEFT JOIN outreach o ON p.id = o.prospect_id
        WHERE {where_clause}
        ORDER BY {order}
        LIMIT ? OFFSET ?
    """
    rows = db.conn.execute(data_sql, params + [per_page, offset]).fetchall()

    # Get distinct sources for filter dropdown
    sources = [r["source"] for r in db.conn.execute(
        f"SELECT DISTINCT source FROM prospects p WHERE source IS NOT NULL AND {hidden_sql} ORDER BY source",
        hidden_params,
    ).fetchall()]

    # Get distinct cities for filter dropdown (top 30 by prospect count)
    city_rows = db.conn.execute(f"""
        SELECT city, COUNT(*) as cnt FROM prospects p
        WHERE city IS NOT NULL AND city != '' AND {hidden_sql}
        GROUP BY city ORDER BY cnt DESC LIMIT 30
    """, hidden_params).fetchall()
    cities_list = [r["city"] for r in city_rows]

    # Pagination
    total_pages = max(1, (total + per_page - 1) // per_page)
    has_prev = page > 1
    has_next = page < total_pages

    # Build base query string for sort links (preserve filters + page)
    from urllib.parse import urlencode
    base_params = {}
    if q:
        base_params["q"] = q
    if source:
        base_params["source"] = source
    if stage:
        base_params["stage"] = stage
    if cities:
        base_params["cities"] = cities
    if campaign:
        base_params["campaign"] = campaign
    if generator_run:
        base_params["generator_run"] = generator_run
    base_qs = urlencode(base_params)

    # Build query string for print link (preserve all filters)
    print_params = dict(base_params)
    print_params["print"] = "1"
    print_qs = urlencode(print_params)

    # Build query string for pagination links
    pg_params = dict(base_params)
    pg_params["sort"] = sort
    pg_params["dir"] = sort_dir.lower()
    pg_qs = urlencode(pg_params)

    saved_lists = db.list_prospect_saved_lists(current_user(request).id)
    for saved in saved_lists:
        saved["url"] = "/prospects?" + urlencode(prospect_list_criteria(saved["criteria"]))
    current_criteria = prospect_list_criteria(dict(q=q, source=source, stage=stage,
        cities=cities, campaign=campaign, generator_run=generator_run, sort=sort, dir=sort_dir.lower(),
        per_page=per_page))
    return templates.TemplateResponse(request, "prospects.html", {
        "saved_lists": saved_lists,
        "current_criteria": current_criteria,
        "prospects": rows,
        "sources": sources,
        "cities_list": cities_list,
        "campaigns": campaign_map,
        "q": q,
        "source_filter": source,
        "stage_filter": stage,
        "cities_filter": cities,
        "campaign_filter": campaign,
        "generator_run_filter": generator_run,
        "sort": sort,
        "sort_dir": sort_dir.lower(),
        "base_qs": base_qs,
        "pg_qs": pg_qs,
        "print_qs": print_qs,
        "page": page,
        "per_page": per_page,
        "total": total,
        "total_pages": total_pages,
        "has_prev": has_prev,
        "has_next": has_next,
    })


@app.get("/calendar", response_class=HTMLResponse)
async def calendar_page(
    request: Request,
    days: int = Query(default=30, ge=1, le=365),
    month: str = Query(default="", description="YYYY-MM to display"),
):
    """Calendar view — visual month grid with all follow-ups and call next-steps."""
    db = get_db()
    from collections import defaultdict
    from datetime import datetime as _dt, timedelta
    import calendar as _calendar

    events = db.get_upcoming_events(days=90, hidden_campaigns=hidden_for(request))  # wider range for month nav

    # Determine which month to display
    today = _dt.now().date()
    if month:
        try:
            display_year, display_month = map(int, month.split("-"))
        except ValueError:
            display_year, display_month = today.year, today.month
    else:
        display_year, display_month = today.year, today.month

    # Build month grid
    cal = _calendar.Calendar(firstweekday=6)  # Sunday first
    month_days = cal.monthdatescalendar(display_year, display_month)

    # Group events by date
    by_date = defaultdict(list)
    for e in events:
        date_key = e["date"].strftime("%Y-%m-%d")
        by_date[date_key].append(e)

    # Flatten days with metadata
    grid_weeks = []
    for week in month_days:
        week_days = []
        for day in week:
            date_key = day.strftime("%Y-%m-%d")
            day_events = by_date.get(date_key, [])
            week_days.append({
                "date": day,
                "date_key": date_key,
                "day_num": day.day,
                "is_today": day == today,
                "is_current_month": day.month == display_month,
                "events": day_events,
                "event_count": len(day_events),
            })
        grid_weeks.append(week_days)

    # Month navigation
    if display_month == 1:
        prev_month = f"{display_year - 1}-12"
        next_month = f"{display_year}-02"
    elif display_month == 12:
        prev_month = f"{display_year}-11"
        next_month = f"{display_year + 1}-01"
    else:
        prev_month = f"{display_year}-{display_month - 1:02d}"
        next_month = f"{display_year}-{display_month + 1:02d}"

    month_name = _dt(display_year, display_month, 1).strftime("%B %Y")
    weekdays = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]

    # Stats for the displayed month
    month_events = [e for e in events if e["date"].year == display_year and e["date"].month == display_month]
    stats = {
        "total": len(events),
        "month_total": len(month_events),
        "followups": sum(1 for e in month_events if e["type"] == "followup"),
        "calls": sum(1 for e in month_events if e["type"] == "call"),
        "today": sum(1 for e in events if e["date"].date() == today),
        "this_week": sum(1 for e in events if e["date"].date() <= (today + timedelta(days=7))),
    }

    feed_url = f"{site_url()}/calendar.ics?days={days}"

    return templates.TemplateResponse(request, "calendar.html", {
        "events": events,
        "grid_weeks": grid_weeks,
        "weekdays": weekdays,
        "month_name": month_name,
        "display_year": display_year,
        "display_month": display_month,
        "prev_month": prev_month,
        "next_month": next_month,
        "today": today,
        "stats": stats,
        "days": days,
        "feed_url": feed_url,
    })


@app.get("/calendar.ics")
async def calendar_ics(request: Request, days: int = Query(default=90, ge=1, le=365)):
    """ICS calendar feed — subscribe in any calendar app."""
    from fastapi.responses import PlainTextResponse
    from core.ics import generate_ics

    db = get_db()
    events = db.get_upcoming_events(days=days, hidden_campaigns=hidden_for(request))
    ics_content = generate_ics(events, calendar_name="agency-os Sales Pipeline")

    return PlainTextResponse(
        content=ics_content,
        media_type="text/calendar; charset=utf-8",
        headers={
            "Content-Disposition": "attachment; filename=agency-os.ics",
            "Cache-Control": "max-age=300",
        },
    )


@app.get("/email-templates", response_class=HTMLResponse)
async def email_templates_page(
    request: Request,
    campaign: str = Query(default=""),
    preview_prospect: int = Query(default=0),
):
    """Email template editor — view, edit, and preview all email scripts."""
    import yaml
    from pathlib import Path

    campaigns = get_campaigns()

    # Load all email scripts (non-phone) from all campaigns
    all_scripts = []
    for c in campaigns:
        scripts_dir = c.config_dir / "scripts"
        if not scripts_dir.exists():
            continue
        for script_file in sorted(scripts_dir.glob("*.yaml")):
            if script_file.name.startswith("phone_"):
                continue
            try:
                script = yaml.safe_load(script_file.read_text())
                script["file_name"] = script_file.stem
                script["file_path"] = str(script_file)
                script["campaign_name"] = c.name
                script["campaign_dir"] = str(c.config_dir)
                all_scripts.append(script)
            except Exception:
                continue

    # Get prospect for preview
    db = get_db()
    preview_p = None
    preview_subject = ""
    preview_body = ""
    if preview_prospect:
        require_visible_prospect(request, preview_prospect)
        preview_p = db.get_prospect(preview_prospect)
        if preview_p:
            # Build variables for preview
            outreach_rows = db.conn.execute(
                """SELECT o.* FROM outreach o WHERE o.prospect_id = ? LIMIT 1""",
                (preview_prospect,),
            ).fetchall()

            for c in campaigns:
                if c.config_dir == Path(all_scripts[0]["campaign_dir"] if all_scripts else "."):
                    variables = {
                        "org_name": preview_p.name,
                        "contact_first": (outreach_rows[0]["contact_name"] or "").split()[0] if outreach_rows and outreach_rows[0]["contact_name"] else "there",
                        "focus_area": (preview_p.focus_area or "civic engagement").replace("_", " "),
                        "city": preview_p.city or "Los Angeles",
                        "state": preview_p.state or "CA",
                        "your_name": c.sender_name,
                        "your_email": c.sender_email,
                    }
                    registry = PluginRegistry()
                    registry.discover("plugins")
                    product = registry.get_product(c.product)
                    if product:
                        variables["demo_link"] = product.generate_demo_link(preview_p) or ""
                        variables["value_prop"] = product.describe_value(preview_p) or ""
                    break

            import re
            def render(text):
                def replace(match):
                    key = match.group(1).strip()
                    return str(variables.get(key, "{{" + key + "}}"))
                return re.sub(r"\{\{(\w+)\}\}", replace, text)

            if all_scripts:
                preview_subject = render(all_scripts[0].get("subject", ""))
                preview_body = render(all_scripts[0].get("body", ""))

    # Get prospects for preview dropdown
    hidden_sql, hidden_params = db.hidden_clause(hidden_for(request))
    prospect_options = db.conn.execute(
        f"""SELECT p.id, p.name, p.city FROM prospects p WHERE {hidden_sql}
           ORDER BY p.name LIMIT 50""", hidden_params,
    ).fetchall()

    return templates.TemplateResponse(request, "email_templates.html", {
        "scripts": all_scripts,
        "prospect_options": prospect_options,
        "preview_prospect": preview_prospect,
        "preview_p": preview_p,
        "preview_subject": preview_subject,
        "preview_body": preview_body,
    })


@app.post("/email-templates/save")
async def save_email_template(
    request: Request,
    file_path: str = Form(...),
    subject: str = Form(default=""),
    body: str = Form(default=""),
):
    """Save an edited email template back to its YAML file."""
    from pathlib import Path
    import yaml

    path = Path(file_path).resolve()
    if (
        not path.exists()
        or not path.name.endswith(".yaml")
        or not path.is_relative_to(CAMPAIGNS_DIR.resolve())
    ):
        raise HTTPException(status_code=400, detail="Invalid file path")

    # Load existing to preserve other keys
    existing = yaml.safe_load(path.read_text()) or {}
    existing["subject"] = subject
    existing["body"] = body

    # Write back
    save_campaign_file(path, yaml.dump(existing, default_flow_style=False, sort_keys=False, allow_unicode=True))
    get_db().audit(current_user(request), "template.save", "template",
                   str(path.relative_to(CAMPAIGNS_DIR.resolve())), {"subject": subject})
    return RedirectResponse(url="/email-templates?saved=1", status_code=303)


@app.get("/email-templates/preview/{script_idx}")
async def preview_template(
    request: Request,
    script_idx: int,
    prospect_id: int = Query(default=0),
):
    """Return a JSON preview of a template rendered for a prospect."""
    import yaml, re, json as _json
    from pathlib import Path

    campaigns = get_campaigns()
    all_scripts = []
    for c in campaigns:
        scripts_dir = c.config_dir / "scripts"
        if not scripts_dir.exists():
            continue
        for script_file in sorted(scripts_dir.glob("*.yaml")):
            if script_file.name.startswith("phone_"):
                continue
            try:
                script = yaml.safe_load(script_file.read_text())
                script["file_name"] = script_file.stem
                script["file_path"] = str(script_file)
                script["campaign_dir"] = str(c.config_dir)
                all_scripts.append(script)
            except Exception:
                continue

    if script_idx >= len(all_scripts):
        return JSONResponse({"error": "Script not found"})

    script = all_scripts[script_idx]
    db = get_db()

    prospect = None
    if prospect_id:
        require_visible_prospect(request, prospect_id)
        prospect = db.get_prospect(prospect_id)

    variables = {}
    if prospect:
        for c in campaigns:
            if str(c.config_dir) == script["campaign_dir"]:
                variables = {
                    "org_name": prospect.name,
                    "contact_first": "there",
                    "focus_area": (prospect.focus_area or "civic engagement").replace("_", " "),
                    "city": prospect.city or "Los Angeles",
                    "state": prospect.state or "CA",
                    "your_name": c.sender_name,
                    "your_email": c.sender_email,
                }
                registry = PluginRegistry()
                registry.discover("plugins")
                product = registry.get_product(c.product)
                if product:
                    variables["demo_link"] = product.generate_demo_link(prospect) or ""
                    variables["value_prop"] = product.describe_value(prospect) or ""
                break

    def render(text):
        def replace(match):
            key = match.group(1).strip()
            return str(variables.get(key, "{{" + key + "}}"))
        return re.sub(r"\{\{(\w+)\}\}", replace, text)

    return JSONResponse({
        "subject": render(script.get("subject", "")),
        "body": render(script.get("body", "")),
    })


# ── Mail templates (Lob direct mail) ────────────────────────────────


def _load_mail_scripts():
    """Load all mail_*.yaml scripts from all campaigns."""
    import yaml
    all_scripts = []
    for c in get_campaigns():
        scripts_dir = c.config_dir / "scripts"
        if not scripts_dir.exists():
            continue
        for script_file in sorted(scripts_dir.glob("mail_*.yaml")):
            try:
                script = yaml.safe_load(script_file.read_text())
                script["file_name"] = script_file.stem
                script["file_path"] = str(script_file)
                script["campaign_name"] = c.name
                script["campaign_dir"] = str(c.config_dir)
                all_scripts.append(script)
            except Exception:
                continue
    return all_scripts


@app.get("/mail-templates", response_class=HTMLResponse)
async def mail_templates_page(
    request: Request,
    preview_prospect: int = Query(default=0),
):
    """Lob mail template editor — view, edit, and preview postcard/letter templates."""
    all_scripts = _load_mail_scripts()
    db = get_db()

    # Get prospect for preview
    preview_p = None
    if preview_prospect:
        require_visible_prospect(request, preview_prospect)
        preview_p = db.get_prospect(preview_prospect)

    # Get prospects for preview dropdown
    hidden_sql, hidden_params = db.hidden_clause(hidden_for(request))
    prospect_options = db.conn.execute(
        f"""SELECT p.id, p.name, p.city FROM prospects p
           WHERE p.address IS NOT NULL AND p.address != '' AND {hidden_sql}
           ORDER BY p.name LIMIT 50""", hidden_params,
    ).fetchall()

    return templates.TemplateResponse(request, "mail_templates.html", {
        "active": "mail-templates",
        "scripts": all_scripts,
        "prospect_options": prospect_options,
        "preview_prospect": preview_prospect,
        "preview_p": preview_p,
        "lob_template_url": lob_template_url,
    })


@app.post("/mail-templates/save")
async def save_mail_template(
    request: Request,
    file_path: str = Form(...),
    front: str = Form(default=""),
    back: str = Form(default=""),
    subject: str = Form(default=""),
    body: str = Form(default=""),
    mail_type: str = Form(default="postcard"),
    front_template_id: str = Form(default=""),
    back_template_id: str = Form(default=""),
    template_id: str = Form(default=""),
):
    """Save an edited mail template back to its YAML file."""
    from pathlib import Path
    import yaml

    path = Path(file_path).resolve()
    if (
        not path.exists()
        or not path.name.endswith(".yaml")
        or not path.is_relative_to(CAMPAIGNS_DIR.resolve())
    ):
        raise HTTPException(status_code=400, detail="Invalid file path")

    # Optional Lob HTML template IDs: when set, Lob renders that design
    design_ids = {
        "front_template_id": front_template_id.strip(),
        "back_template_id": back_template_id.strip(),
        "template_id": template_id.strip(),
    }
    bad = [v for v in design_ids.values() if v and not TEMPLATE_ID_RE.match(v)]
    if bad:
        return RedirectResponse(
            url=f"/mail-templates?error={quote(f'{bad[0]} is not a Lob template ID (they look like tmpl_…)')}",
            status_code=303,
        )

    existing = yaml.safe_load(path.read_text()) or {}
    existing["mail_type"] = mail_type
    if mail_type == "letter":
        existing["subject"] = subject
        existing["body"] = body
        # Remove postcard fields if they exist
        for key in ("front", "back", "front_template_id", "back_template_id"):
            existing.pop(key, None)
        design_keys = ("template_id",)
    else:
        existing["front"] = front
        existing["back"] = back
        # Remove letter fields if they exist
        for key in ("subject", "body", "template_id"):
            existing.pop(key, None)
        design_keys = ("front_template_id", "back_template_id")
    for key in design_keys:
        if design_ids[key]:
            existing[key] = design_ids[key]
        else:
            existing.pop(key, None)

    save_campaign_file(path, yaml.dump(existing, default_flow_style=False, sort_keys=False, allow_unicode=True))
    get_db().audit(current_user(request), "mail_template.save", "mail_template",
                   str(path.relative_to(CAMPAIGNS_DIR.resolve())), {"mail_type": mail_type})
    return RedirectResponse(url="/mail-templates?saved=1", status_code=303)


@app.get("/mail-templates/preview/{script_idx}")
async def preview_mail_template(
    request: Request,
    script_idx: int,
    prospect_id: int = Query(default=0),
):
    """Return a JSON preview of a mail template rendered for a prospect."""
    import re
    all_scripts = _load_mail_scripts()

    if script_idx >= len(all_scripts):
        return JSONResponse({"error": "Mail template not found"})

    script = all_scripts[script_idx]
    db = get_db()

    prospect = None
    if prospect_id:
        require_visible_prospect(request, prospect_id)
        prospect = db.get_prospect(prospect_id)

    variables = {}
    if prospect:
        for c in get_campaigns():
            if str(c.config_dir) == script["campaign_dir"]:
                variables = {
                    "org_name": prospect.name,
                    "contact_first": "there",
                    "focus_area": (prospect.focus_area or "civic engagement").replace("_", " "),
                    "city": prospect.city or "Los Angeles",
                    "state": prospect.state or "CA",
                    "your_name": c.sender_name,
                    "your_email": c.sender_email,
                }
                registry = PluginRegistry()
                registry.discover("plugins")
                product = registry.get_product(c.product)
                if product:
                    variables["demo_link"] = product.generate_demo_link(prospect) or ""
                    variables["value_prop"] = product.describe_value(prospect) or ""
                break

    def render(text):
        def replace(match):
            key = match.group(1).strip()
            return str(variables.get(key, "{{" + key + "}}"))
        return re.sub(r"\{\{(\w+)\}\}", replace, text)

    mail_type = script.get("mail_type", "postcard")
    if mail_type == "letter":
        return JSONResponse({
            "mail_type": "letter",
            "subject": render(script.get("subject", "")),
            "body": render(script.get("body", "")),
        })
    else:
        return JSONResponse({
            "mail_type": "postcard",
            "front": render(script.get("front", "")),
            "back": render(script.get("back", "")),
        })


@app.get("/call-log", response_class=HTMLResponse)
async def call_log_page(
    request: Request,
    outcome: str = Query(default=""),
    interest: str = Query(default=""),
    page: int = Query(default=1, ge=1),
    per_page: int = Query(default=50, ge=10, le=200),
):
    """Call log viewer — all calls across all campaigns."""
    db = get_db()
    offset = (page - 1) * per_page
    hidden = hidden_for(request)
    calls = db.get_all_calls(limit=per_page, offset=offset, outcome=outcome, interest=interest,
                             hidden_campaigns=hidden)

    # Count total
    hidden_sql, params = db.hidden_clause(hidden, "cl.prospect_id")
    where_parts = [hidden_sql]
    if outcome:
        where_parts.append("cl.outcome = ?")
        params.append(outcome)
    if interest:
        where_parts.append("cl.interest_level = ?")
        params.append(interest)
    where_clause = " AND ".join(where_parts) if where_parts else "1=1"
    total = db.conn.execute(
        f"""SELECT COUNT(*) FROM call_log cl
           JOIN campaigns c ON cl.campaign_id = c.id WHERE {where_clause}""",
        params,
    ).fetchone()[0]

    stats = db.get_call_stats(hidden_campaigns=hidden)
    total_pages = max(1, (total + per_page - 1) // per_page)

    return templates.TemplateResponse(request, "call_log.html", {
        "calls": calls,
        "stats": stats,
        "outcome_filter": outcome,
        "interest_filter": interest,
        "page": page,
        "total": total,
        "total_pages": total_pages,
        "has_prev": page > 1,
        "has_next": page < total_pages,
    })


@app.post("/call-log/record")
async def record_call(
    request: Request,
    prospect_id: int = Form(...),
    outreach_id: int = Form(...),
    campaign_id: int = Form(...),
    script_key: str = Form(default=""),
    script_title: str = Form(default=""),
    stage_at_call: str = Form(default=""),
    outcome: str = Form(default="completed"),
    duration_minutes: int = Form(default=0),
    interest_level: str = Form(default=""),
    decision_maker_name: str = Form(default=""),
    decision_maker_role: str = Form(default=""),
    next_step: str = Form(default=""),
    next_step_date: str = Form(default=""),
    voicemail_left: str = Form(default=""),
    notes: str = Form(default=""),
    voice_call_id: int = Form(default=0),
):
    """Record a completed phone call (and link it to the dashboard call it came from, if any)."""
    from core.models import CallLog
    require_visible_prospect(request, prospect_id)
    if outcome not in contact_depth.CALL_OUTCOMES:
        return _back(f"/prospects/{prospect_id}", error="Pick a call outcome from the list.")
    db = get_db()
    if voice_call_id:
        placed = db.get_voice_call(voice_call_id)
        if (not placed or placed["user_id"] != current_user(request).id
                or placed["outreach_id"] != outreach_id or placed["call_log_id"]):
            return _back(f"/prospects/{prospect_id}", error="That dashboard call isn't yours, or it's already logged.")

    from datetime import datetime as _dt
    nsd = None
    if next_step_date:
        try:
            nsd = _dt.fromisoformat(next_step_date)
        except ValueError:
            nsd = None

    call = CallLog(
        outreach_id=outreach_id,
        campaign_id=campaign_id,
        prospect_id=prospect_id,
        script_key=script_key or None,
        script_title=script_title or None,
        stage_at_call=stage_at_call or None,
        outcome=outcome,
        duration_minutes=duration_minutes or None,
        interest_level=interest_level or None,
        decision_maker_name=decision_maker_name or None,
        decision_maker_role=decision_maker_role or None,
        next_step=next_step or None,
        next_step_date=nsd,
        voicemail_left=bool(voicemail_left),
        notes=notes or None,
        called_by=current_user(request).name,
        called_by_user_id=current_user(request).id,
    )
    call_id = db.log_call(call)
    if voice_call_id:
        db.link_voice_call(voice_call_id, call_id, current_user(request).id)
    return RedirectResponse(url=f"/prospects/{prospect_id}", status_code=303)


@app.get("/call-scripts", response_class=HTMLResponse)
async def call_scripts(
    request: Request,
    prospect_id: int = Query(default=0, description="Personalize scripts for this prospect"),
    stage: str = Query(default="", description="Filter by pipeline stage"),
    print: int = Query(default=0, description="Print view"),
):
    """Phone call script viewer — personalized per prospect, printable."""
    db = get_db()
    campaigns = get_campaigns()

    # Load all phone script YAMLs from all campaigns
    import yaml
    from pathlib import Path

    all_scripts = []
    for c in campaigns:
        scripts_dir = c.config_dir / "scripts"
        if not scripts_dir.exists():
            continue
        for script_file in sorted(scripts_dir.glob("phone_*.yaml")):
            try:
                script = yaml.safe_load(script_file.read_text())
                script["campaign_name"] = c.name
                script["file_name"] = script_file.stem
                all_scripts.append(script)
            except Exception:
                continue

    # Filter by stage if requested
    if stage:
        all_scripts = [s for s in all_scripts if s.get("stage") == stage]

    # Personalize for a specific prospect
    prospect = None
    if prospect_id:
        require_visible_prospect(request, prospect_id)
        prospect = db.get_prospect(prospect_id)
        if prospect:
            # Get outreach info
            outreach_rows = db.conn.execute(
                """SELECT o.*, c.name as campaign_name FROM outreach o
                   JOIN campaigns c ON o.campaign_id = c.id
                   WHERE o.prospect_id = ? ORDER BY o.updated_at DESC""",
                (prospect_id,),
            ).fetchall()

            # Build template variables
            from core.pipeline import Pipeline
            pipeline = Pipeline(db, PluginRegistry())
            # Find the campaign config for this prospect
            campaign_config = None
            for c in campaigns:
                for o in outreach_rows:
                    if c.db_name == o["campaign_name"]:
                        campaign_config = c
                        break
                if campaign_config:
                    break

            variables = {
                "org_name": prospect.name,
                "contact_name": outreach_rows[0]["contact_name"] if outreach_rows else prospect.name,
                "contact_first": (outreach_rows[0]["contact_name"] or "").split()[0] if outreach_rows and outreach_rows[0]["contact_name"] else "there",
                "focus_area": (prospect.focus_area or "civic engagement").replace("_", " "),
                "city": prospect.city or "Los Angeles",
                "state": prospect.state or "CA",
                "your_name": campaign_config.sender_name if campaign_config else "",
                "voter_status": "active" if prospect.voter_engagement else "emerging",
                # Filled in by enrichment; the fallbacks read as notes to the rep
                "contact_title": (outreach_rows[0]["contact_title"] if outreach_rows else None) or "title unknown",
                "website": prospect.website_url or "no website on file",
                "site_summary": prospect.site_summary or "no site summary yet (run enrichment to pull one)",
            }

            # Get demo link from product plugin
            if campaign_config:
                registry = PluginRegistry()
                registry.discover("plugins")
                product = registry.get_product(campaign_config.product)
                if product:
                    variables["demo_link"] = product.generate_demo_link(prospect) or ""
                    variables["value_prop"] = product.describe_value(prospect) or ""

            # Render scripts with variables
            import re
            def render(text):
                def replace(match):
                    key = match.group(1).strip()
                    return str(variables.get(key, match.group(0)))
                return re.sub(r"\{\{(\w+)\}\}", replace, text)

            for s in all_scripts:
                s["body_rendered"] = render(s.get("body", ""))
                s["title_rendered"] = render(s.get("title", ""))
                s["prospect_name"] = prospect.name
                s["prospect_phone"] = outreach_rows[0]["contact_phone"] if outreach_rows else None
                s["prospect_email"] = outreach_rows[0]["contact_email"] if outreach_rows else None
                s["prospect_org"] = prospect.name
                s["prospect_website"] = prospect.website_url

    from datetime import datetime as _dt

    # Build prospect options for the dropdown (top 100 by name)
    prospect_options = []
    if not prospect_id:
        hidden_sql, hidden_params = db.hidden_clause(hidden_for(request))
        prospect_options = db.conn.execute(
            f"""SELECT p.id, p.name, p.city FROM prospects p
               JOIN outreach o ON p.id = o.prospect_id
               WHERE (o.contact_phone IS NOT NULL OR o.contact_email IS NOT NULL) AND {hidden_sql}
               ORDER BY p.name LIMIT 100""", hidden_params,
        ).fetchall()

    voice_button = None
    if prospect and outreach_rows and not print:
        voice_button = _voice_buttons(request, outreach_rows[:1], prospect,
                                      db.do_not_call(prospect.id)).get(outreach_rows[0]["id"])

    template_name = "call_scripts_print.html" if print else "call_scripts.html"

    return templates.TemplateResponse(request, template_name, {
        "scripts": all_scripts,
        "prospect": prospect,
        "prospect_options": prospect_options,
        "stage_filter": stage,
        "now": _dt.now().strftime("%B %d, %Y at %I:%M %p"),
        "voice_button": voice_button,
    })


@app.get("/api/prospects/{prospect_id}/neighbors")
async def prospect_neighbors(
    request: Request,
    prospect_id: int,
    q: str = "", source: str = "", stage: str = "", cities: str = "", campaign: str = "",
    sort: str = "name", dir: str = "asc",
):
    """The previous and next prospect in the list the user came from (same
    filters and sort as /prospects), so the detail page can move between records."""
    db = get_db()
    where, params = db.prospect_filter(
        {"q": q, "source": source, "stage": stage, "campaign": campaign, "cities": cities}, hidden_for(request))
    order, _ = prospect_list_order(sort, dir, sort in request.query_params)
    order += ", p.id"
    row = db.conn.execute(f"""
        SELECT prev_id, next_id, position, total FROM (
            SELECT p.id,
                   LAG(p.id) OVER (ORDER BY {order}) AS prev_id,
                   LEAD(p.id) OVER (ORDER BY {order}) AS next_id,
                   ROW_NUMBER() OVER (ORDER BY {order}) AS position,
                   COUNT(*) OVER () AS total
            FROM prospects p
            LEFT JOIN outreach o ON p.id = o.prospect_id
            WHERE {where}
        ) ranked WHERE id = ? ORDER BY position LIMIT 1
    """, params + [prospect_id]).fetchone()
    if not row:  # not in that list (filters changed since): no neighbors
        return {"in_list": False}
    return {"in_list": True, "prev": row["prev_id"], "next": row["next_id"],
            "position": row["position"], "total": row["total"]}


@app.get("/prospects/{prospect_id}", response_class=HTMLResponse)
async def prospect_detail(request: Request, prospect_id: int):
    """Prospect detail — info, outreach timeline, email log."""
    db = get_db()
    prospect = db.get_prospect(prospect_id)
    if not prospect:
        raise HTTPException(status_code=404, detail="Prospect not found")

    # Get all outreach rows for this prospect
    outreach_rows = db.conn.execute(
        """SELECT o.*, c.name as campaign_name FROM outreach o
           JOIN campaigns c ON o.campaign_id = c.id
           WHERE o.prospect_id = ? ORDER BY o.updated_at DESC""",
        (prospect_id,),
    ).fetchall()

    # Get email logs for all outreach rows
    outreach_ids = [r["id"] for r in outreach_rows]
    email_logs = []
    if outreach_ids:
        placeholders = ",".join("?" * len(outreach_ids))
        email_logs = db.conn.execute(
            f"""SELECT e.*, c.name as campaign_name FROM email_log e
               JOIN campaigns c ON e.campaign_id = c.id
               WHERE e.outreach_id IN ({placeholders})
               ORDER BY e.sent_at DESC""",
            outreach_ids,
        ).fetchall()

    # Parse activity logs
    for o in outreach_rows:
        o_keys = dict(o)
        activity = json.loads(o["activity_log"] or "[]")
        # We can't modify Row, so we'll pass separately

    # Get call history for this prospect
    call_history = db.get_calls_for_prospect(prospect_id)

    # Demo page (A6): status kept on the prospect by provisioning and
    # pull-events, plus the portal events logged on the outreach rows, both
    # under the product's portal_namespace.
    activity_logs = {r["id"]: json.loads(r["activity_log"] or "[]") for r in outreach_rows}
    _, portal_product_ = _portal_campaign(outreach_rows) or (None, None)
    namespace = portal_product_.portal_namespace if portal_product_ else None
    portal_events = sorted(
        (e for log in activity_logs.values() for e in log
         if namespace and str(e.get("ref", "")).startswith(f"{namespace}:")),
        key=lambda e: e.get("timestamp", ""), reverse=True,
    )

    do_not_call = db.do_not_call(prospect_id)
    voice_buttons = _voice_buttons(request, outreach_rows, prospect, do_not_call)
    voice_prefill = None
    if request.query_params.get("voice_call", "").isdigit():
        placed = db.get_voice_call(int(request.query_params["voice_call"]))
        if (placed and placed["user_id"] == current_user(request).id
                and placed["prospect_id"] == prospect_id and not placed["call_log_id"]):
            voice_prefill = {**voice.log_prefill(placed), "script_key": request.query_params.get("script", "")}

    lead_package = lead_packages.package_info(prospect)
    if lead_package:
        row = db.get_lead_package(int(lead_package["lead_package_id"]))
        lead_package = {**lead_package, "title": row["title"] if row else lead_package.get("package_id")}
    return templates.TemplateResponse(request, "prospect_detail.html", {
        "plugin_panels": await render_plugin_panels(request, "prospect", prospect),
        "board": panels.Board("prospect", panels.load(db, current_user(request).id, "prospect")),
        "stage_background": panels.STAGE_BACKGROUNDS.get(outreach_rows[0]["stage"]) if outreach_rows else None,
        "contact_tier": contact_depth.history_for_prospect(db, prospect_id)[1],
        "lead_package": lead_package,
        "verification": verify.lead_verdict(db, prospect_id) if lead_package else None,
        "credit": royalties.credits(db, prospect_id) if current_user(request).can("packages.sell") else None,
        "credit_labels": royalties.TASK_LABELS,
        "team": [u for u in db.list_users() if u.get("is_active")] if current_user(request).can("packages.sell") else [],
        "do_not_sell": bool(db.conn.execute("SELECT do_not_sell FROM prospects WHERE id = ?",
                                            (prospect_id,)).fetchone()["do_not_sell"]),
        "do_not_call": do_not_call,
        "voice_buttons": voice_buttons,
        "voice_prefill": voice_prefill,
        "agent_panel": {"personas": list(agents.load_personas().values()),
                        "tasks": agents.all_tasks(agents.load_personas().values()),
                        "model": llm.describe()} if current_user(request).uses_ai("agents.use") else None,
        "bounced": verify.bounced_emails(db, prospect_id),
        "package_spend": db.prospect_spend(prospect_id),
        "portal": ((prospect.metadata or {}).get(namespace) or {}) if namespace else {},
        "portal_label": portal_product_.portal_label if portal_product_ else "Demo page",
        "portal_link": next((r["demo_link"] for r in outreach_rows if r["demo_link"]), None),
        "portal_events": portal_events,
        "ready_to_close": any(e.get("flag") == "ready_to_close" for e in portal_events),
        "portal_available": portal_product_ is not None,
        "msg": request.query_params.get("msg", ""),
        "error": request.query_params.get("error", ""),
        "prospect": prospect,
        "outreach_rows": outreach_rows,
        "email_logs": email_logs,
        "call_history": call_history,
        "activity_logs": activity_logs,
    })


def _voice_buttons(request: Request, outreach_rows, prospect, do_not_call: bool) -> dict[int, dict]:
    """Outreach id → the softphone Call button, for rows this user dials from the
    dashboard (core/voice.py). Rows not in it keep their tel: link."""
    user = current_user(request)
    campaigns = {c.db_name: c for c in get_campaigns()}
    buttons = {}
    for o in outreach_rows:
        campaign = campaigns.get(o["campaign_name"])
        button = campaign and voice.call_button(user, campaign, o["contact_phone"], prospect, do_not_call)
        if button:
            buttons[o["id"]] = {**button, "outreach_id": o["id"], "prospect_id": prospect.id}
    return buttons


def _plugin_registry() -> PluginRegistry:
    registry = PluginRegistry()
    registry.discover(str(PROJECT_ROOT / "plugins"))
    return registry


def _portal_campaign(outreach_rows):
    """(campaign, product) for the first of the prospect's campaigns whose
    product makes demo pages, or None."""
    names = {r["campaign_name"] for r in outreach_rows}
    registry = _plugin_registry()
    for campaign in get_campaigns():
        product = portal_product(registry.get_product(campaign.product)) if campaign.db_name in names else None
        if product:
            return campaign, product
    return None


@app.post("/prospects/{prospect_id}/portal")
async def prospect_portal(request: Request, prospect_id: int, action: str = Form(...)):
    """Create, renew, or check a prospect's demo page (A6)."""
    db = get_db()
    rows = db.conn.execute(
        """SELECT o.*, c.name as campaign_name FROM outreach o
           JOIN campaigns c ON o.campaign_id = c.id WHERE o.prospect_id = ?""",
        (prospect_id,),
    ).fetchall()
    found = _portal_campaign(rows)
    back = f"/prospects/{prospect_id}"
    if found is None:
        return _back(back, error="None of this prospect's campaigns make demo pages.")
    campaign, product = found

    if action not in ("provision", "renew", "status"):
        return _back(back, error="Unknown action.")

    def call_product() -> dict:
        # Runs in a worker thread (the API call can take seconds); database
        # connections are per thread, so open one here.
        pipeline = Pipeline(get_db(), _plugin_registry())
        if action == "status":
            return pipeline.refresh_portal_status(campaign, prospect_id)
        return pipeline.provision_prospect(campaign, prospect_id, refresh=action == "renew")

    result = await asyncio.to_thread(call_product)
    done = {"provision": "Demo page ready.", "renew": "Demo page renewed with a new link.",
            "status": "Status updated."}[action]

    if result.get("error"):
        return _back(back, error=f"{product.portal_label}: {result.get('detail') or 'request failed'}")
    db.audit(current_user(request), f"prospect.portal.{action}", "prospect", prospect_id,
             {"status": result.get("status"), "slug": result.get("slug")})
    return _back(back, msg=done)


@app.post("/prospects/{prospect_id}/stage")
async def update_stage(
    request: Request,
    prospect_id: int,
    outreach_id: int = Form(...),
    stage: str = Form(...),
    notes: str = Form(default=""),
):
    """Update a prospect's pipeline stage."""
    db = get_db()
    updates = {"stage": stage}
    if notes:
        updates["notes"] = notes
    if stage in ("closed_won", "closed_lost"):
        updates["closed_at"] = datetime.now().isoformat()
        if notes:
            updates["close_reason"] = notes
    db.update_outreach(outreach_id, updates)
    db.audit(current_user(request), "prospect.stage", "outreach", outreach_id,
             {"prospect_id": prospect_id, "stage": stage, "notes": notes})
    return RedirectResponse(url=f"/prospects/{prospect_id}", status_code=303)


@app.post("/prospects/{prospect_id}/contact")
async def update_contact(
    request: Request,
    prospect_id: int,
    outreach_id: int = Form(...),
    contact_name: str = Form(default=""),
    contact_email: str = Form(default=""),
    contact_phone: str = Form(default=""),
    contact_title: str = Form(default=""),
):
    """Manually update contact info for a prospect."""
    db = get_db()
    updates = {}
    if contact_name:
        updates["contact_name"] = contact_name
    if contact_email:
        updates["contact_email"] = contact_email
    if contact_phone:
        updates["contact_phone"] = contact_phone
    if contact_title:
        updates["contact_title"] = contact_title
    if updates:
        db.update_outreach(outreach_id, updates)
        db.audit(current_user(request), "prospect.contact", "outreach", outreach_id,
                 {"prospect_id": prospect_id, **updates})
    return RedirectResponse(url=f"/prospects/{prospect_id}", status_code=303)


@app.post("/prospects/{prospect_id}/info")
async def update_prospect_info(
    request: Request,
    prospect_id: int,
    name: str = Form(default=""),
    website_url: str = Form(default=""),
    address: str = Form(default=""),
    city: str = Form(default=""),
    state: str = Form(default=""),
    zip_code: str = Form(default=""),
    focus_area: str = Form(default=""),
    voter_engagement: str = Form(default=""),
):
    """Update prospect organization info."""
    db = get_db()
    updates = {}
    if name:
        updates["name"] = name
    if website_url:
        updates["website_url"] = website_url
    if address:
        updates["address"] = address
    if city:
        updates["city"] = city
    if state:
        updates["state"] = state
    if zip_code:
        updates["zip"] = zip_code
    if focus_area:
        updates["focus_area"] = focus_area
    if voter_engagement:
        updates["voter_engagement"] = 1 if voter_engagement == "yes" else 0

    if updates:
        c = db.conn
        sets = []
        vals = []
        for k, v in updates.items():
            sets.append(f"{k} = ?")
            vals.append(v)
        sets.append("updated_at = CURRENT_TIMESTAMP")
        vals.append(prospect_id)
        c.execute(f"UPDATE prospects SET {', '.join(sets)} WHERE id = ?", vals)
        db.audit(current_user(request), "prospect.info", "prospect", prospect_id, updates)

    return RedirectResponse(url=f"/prospects/{prospect_id}", status_code=303)


@app.get("/campaigns", response_class=HTMLResponse)
async def campaign_list(request: Request):
    """Campaign overview page."""
    db = get_db()
    campaigns = visible_campaigns(request)

    show_spend = current_user(request).can("spend.view")
    campaign_data = []
    for c in campaigns:
        stats = db.get_pipeline_stats(c.db_name)
        spend = None
        campaign_id = db.get_campaign_id(c.db_name) if show_spend and c.lead_packages else None
        if campaign_id:
            spend = db.campaign_spend(campaign_id)
            spend["policy"] = payments.SpendPolicy.from_config(c.lead_packages)
        campaign_data.append({
            "config": c,
            "stats": stats,
            "spend": spend,
        })

    return templates.TemplateResponse(request, "campaigns.html", {
        "campaigns": campaign_data,
    })


# ── Lead packages (x402) ────────────────────────────────────────────


def _usd(atomic) -> str:
    return f"${payments.atomic_to_usd(atomic):,.2f}"


templates.env.filters["usd"] = _usd
templates.env.globals["explorer_url"] = payments.explorer_url
templates.env.globals["WEIGHT_LABELS"] = verify.WEIGHT_LABELS
templates.env.globals["UNWORKED_POLICIES"] = verify.UNWORKED_POLICIES


@app.get("/lead-packages", response_class=HTMLResponse)
async def lead_packages_page(request: Request, msg: str = Query(default=""), error: str = Query(default="")):
    """Browse approved providers' lead packages and the ones already unlocked."""
    user = current_user(request)
    db = get_db()
    catalogs = []
    for provider in lead_packages.configured_providers():
        packages, problem = await run_in_threadpool(lead_packages.cached_catalog, provider)
        catalogs.append({"provider": provider, "packages": packages, "error": problem})
    buyable = [c for c in visible_campaigns(request) if (c.lead_packages or {}).get("enabled")]
    hidden = hidden_for(request)
    return templates.TemplateResponse(request, "lead_packages.html", {
        "ratings": claims.ratings(db),
        "active": "lead-packages",
        "catalogs": catalogs,
        "campaigns": buyable,
        "unlocked": [lp for lp in db.list_lead_packages() if lp.get("campaign_name") not in hidden]
                    if user.can("spend.view") else [],
        "payments_on": payments.payments_enabled(),
        "allowance": db.spend_allowance(user.id),
        "msg": msg,
        "error": error,
    })


def _package_campaign(request: Request, provider: str, campaign: str):
    """(campaign config, campaign id, error) for buying from `provider` into `campaign`."""
    if provider not in lead_packages.configured_providers():
        return None, None, "That provider isn't on the approved list."
    config = next((c for c in visible_campaigns(request) if c.db_name == campaign), None)
    if config is None or not (config.lead_packages or {}).get("enabled"):
        return None, None, "Pick a campaign that has lead packages turned on."
    db = get_db()
    campaign_id = db.get_campaign_id(config.db_name) or db.upsert_campaign(
        config.db_name, str(config.config_dir / "campaign.yaml"))
    return config, campaign_id, ""


@app.get("/lead-packages/review", response_class=HTMLResponse)
async def lead_package_review(request: Request, provider: str = Query(...), package_id: str = Query(...),
                              campaign: str = Query(...)):
    """The confirm screen: what the unlock costs and what's left to spend. Never pays."""
    config, campaign_id, error = _package_campaign(request, provider, campaign)
    if error:
        return _back("/lead-packages", error=error)
    package, problem = await run_in_threadpool(lead_packages.find_package, provider, package_id)
    if package is None:
        return _back("/lead-packages", error=problem)
    preview = lead_packages.preview_unlock(get_db(), config, campaign_id, current_user(request), package)
    return templates.TemplateResponse(request, "lead_package_review.html", {
        "active": "lead-packages",
        "preview": preview,
        "campaign": config,
        "network_name": {payments.BASE_SEPOLIA: "Base Sepolia (test USDC)",
                         payments.BASE_MAINNET: "Base (real USDC)"}.get(package.network, package.network),
    })


@app.post("/lead-packages/unlock")
async def lead_package_unlock(
    request: Request,
    provider: str = Form(...),
    package_id: str = Form(...),
    campaign: str = Form(...),
    confirm: str = Form(default=""),
):
    """Pay for a package and import its leads. Needs an explicit confirm."""
    user = current_user(request)
    if confirm != "yes":
        return _back("/lead-packages", error="Tick the box to approve the payment.")
    config, campaign_id, error = _package_campaign(request, provider, campaign)
    if error:
        return _back("/lead-packages", error=error)
    db = get_db()

    def run():
        package, problem = lead_packages.find_package(provider, package_id)
        if package is None:
            return lead_packages.UnlockResult(False, problem)
        return lead_packages.unlock(db, config, campaign_id, user, package)

    result = await run_in_threadpool(run)
    if result.ok:
        return _back("/lead-packages", msg=result.message)
    return _back("/lead-packages", error=result.message)


@app.get("/lead-packages/unlocked/{lead_package_id}", response_class=HTMLResponse)
async def lead_package_detail(request: Request, lead_package_id: int,
                              msg: str = Query(default=""), error: str = Query(default="")):
    """An unlocked package's guarantee: each lead's verdict, the measured rate, claims."""
    status = await run_in_threadpool(claims.package_status, get_db(), lead_package_id)
    if status is None:
        raise HTTPException(status_code=404, detail="Package not found")
    return templates.TemplateResponse(request, "lead_package_detail.html", {
        "active": "lead-packages", **status, "msg": msg, "error": error,
        "ai_review": verify.default_reviewer().is_configured(),
    })


@app.post("/lead-packages/unlocked/{lead_package_id}/verify")
async def lead_package_verify(request: Request, lead_package_id: int):
    """Re-check every lead now, including the AI review when it's turned on."""
    db = get_db()
    if db.get_lead_package(lead_package_id) is None:
        raise HTTPException(status_code=404, detail="Package not found")
    await run_in_threadpool(verify.evaluate_package, db, lead_package_id, reviewer=verify.default_reviewer())
    db.audit(current_user(request), "lead_package.verify", "lead_package", lead_package_id, {})
    return _back(f"/lead-packages/unlocked/{lead_package_id}", msg="Leads re-checked.")


@app.post("/lead-packages/unlocked/{lead_package_id}/claim")
async def lead_package_claim(request: Request, lead_package_id: int, confirm: str = Form(default="")):
    """File a guarantee claim with the provider. Needs an explicit confirm."""
    back = f"/lead-packages/unlocked/{lead_package_id}"
    if confirm != "yes":
        return _back(back, error="Tick the box to file the claim.")
    result = await run_in_threadpool(claims.file_claim, get_db(), lead_package_id, current_user(request))
    return _back(back, msg=result.message) if result.ok else _back(back, error=result.message)


@app.post("/prospects/{prospect_id}/contact-event")
async def prospect_contact_event(request: Request, prospect_id: int, kind: str = Form(...),
                                 outreach_id: int = Form(...)):
    """Record a bounce or returned mail against the contact we have on file."""
    db = get_db()
    row = db.conn.execute("SELECT * FROM outreach WHERE id = ? AND prospect_id = ?",
                          (outreach_id, prospect_id)).fetchone()
    if row is None or kind not in ("email_bounced", "mail_returned"):
        return _back(f"/prospects/{prospect_id}", error="Unknown contact or event.")
    value = row["contact_email"] if kind == "email_bounced" else (db.get_prospect(prospect_id).address or "")
    if kind == "email_bounced" and not value:
        return _back(f"/prospects/{prospect_id}", error="There's no email on file to mark as bounced.")
    verify.record_event(db, prospect_id, kind, value or "", campaign_id=row["campaign_id"],
                        user=current_user(request))
    return _back(f"/prospects/{prospect_id}", msg=f"{verify.EVENT_KINDS[kind]}: recorded.")


@app.post("/prospects/{prospect_id}/refresh-email")
async def prospect_refresh_email(request: Request, prospect_id: int, outreach_id: int = Form(...)):
    """Run the campaign's enrichers to find a new email and compare with the package's."""
    db = get_db()
    prospect = db.get_prospect(prospect_id)
    row = db.conn.execute(
        """SELECT o.*, c.name AS campaign_name FROM outreach o JOIN campaigns c ON c.id = o.campaign_id
           WHERE o.id = ? AND o.prospect_id = ?""", (outreach_id, prospect_id)).fetchone()
    campaign = next((c for c in get_campaigns() if row and c.db_name == row["campaign_name"]), None)
    if prospect is None or campaign is None:
        return _back(f"/prospects/{prospect_id}", error="Campaign not found for this contact.")
    message = await run_in_threadpool(verify.refresh_email, db, _plugin_registry(), campaign, prospect,
                                      dict(row), user=current_user(request))
    return _back(f"/prospects/{prospect_id}", msg=message)


# ── AI: agent panel and the user's own assistant (core/tools.py) ────

_AI_OFF = "Turn on AI features on your Account page first."
_MAX_TOOL_BODY = 64 * 1024
MAX_WORKFLOW_UPLOAD = 1024 * 1024


def _ai_refusal(request: Request, permission: str) -> Optional[JSONResponse]:
    """A JSON refusal unless this user has the AI feature on; the header blocks cross-site posts."""
    if not current_user(request).uses_ai(permission):
        return JSONResponse({"ok": False, "error": _AI_OFF}, status_code=403)
    if request.method == "POST" and request.headers.get("x-aos-tool") != "1":
        return JSONResponse({"ok": False, "error": "Missing X-AOS-Tool header"}, status_code=400)
    return None


@app.post("/prospects/{prospect_id}/agent")
async def prospect_agent(request: Request, prospect_id: int, agent: str = Form(...), task: str = Form(...),
                         instructions: str = Form(default="")):
    """Draft with a sales persona for this prospect. Returns JSON; nothing is sent."""
    refusal = _ai_refusal(request, "agents.use")
    if refusal:
        return refusal
    result = await run_in_threadpool(
        tools.run_tool, get_db(), current_user(request), "draft_with_agent",
        {"prospect_id": prospect_id, "agent": agent, "task": task, "instructions": instructions}, source="panel")
    return JSONResponse(result)


@app.post("/prospects/{prospect_id}/agent/note")
async def prospect_agent_note(request: Request, prospect_id: int, note: str = Form(...)):
    """Save a draft from the panel as a note. Clicking Save is the confirmation."""
    refusal = _ai_refusal(request, "agents.use")
    if refusal:
        return refusal
    result = tools.run_tool(get_db(), current_user(request), "add_prospect_note",
                            {"prospect_id": prospect_id, "note": note}, source="panel", confirmed=True)
    return JSONResponse(result)


_MAX_CHAT_BODY = 256 * 1024


@app.post("/agent/chat")
async def agent_chat(request: Request):
    """The chat robot: a persona's next reply. Body: {"agent", "messages": [{role, content}...], "prospect_id"?}.
    Returns JSON; the conversation lives in the browser and nothing is sent or saved."""
    refusal = _ai_refusal(request, "agents.use")
    if refusal:
        return refusal
    raw = await request.body()
    if len(raw) > _MAX_CHAT_BODY:
        return JSONResponse({"ok": False, "error": "This conversation is too long; start a new one"}, status_code=413)
    try:
        body = json.loads(raw or b"{}")
    except ValueError:
        body = None
    if not isinstance(body, dict) or not isinstance(body.get("agent"), str):
        return JSONResponse({"ok": False, "error": "Body must be a JSON object with an agent"}, status_code=400)
    user, prospect_id = current_user(request), body.get("prospect_id")
    context = None
    if prospect_id is not None:
        if not isinstance(prospect_id, int) or isinstance(prospect_id, bool):
            return JSONResponse({"ok": False, "error": "prospect_id must be a number"}, status_code=400)
        try:
            context = await run_in_threadpool(tools.build_prospect_context, get_db(), user, prospect_id)
        except tools.ToolError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=404)
    result = await run_in_threadpool(agents.chat, body["agent"], body.get("messages"), context)
    return JSONResponse(result)


@app.get("/api/tools")
async def api_tools(request: Request):
    """The tools this user's own AI may call (WebMCP)."""
    refusal = _ai_refusal(request, "ai.connect")
    if refusal:
        return refusal
    return {"tools": [t.public() for t in tools.available(current_user(request))]}


@app.post("/api/tools/{tool_name}")
async def api_tool_call(request: Request, tool_name: str):
    """Run one tool for the user's own AI. Body: {"args": {...}, "confirmed": bool}."""
    refusal = _ai_refusal(request, "ai.connect")
    if refusal:
        return refusal
    raw = await request.body()
    if len(raw) > _MAX_TOOL_BODY:
        return JSONResponse({"ok": False, "error": "Request too large"}, status_code=413)
    try:
        body = json.loads(raw or b"{}")
    except ValueError:
        return JSONResponse({"ok": False, "error": "Body must be JSON"}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"ok": False, "error": "Body must be a JSON object"}, status_code=400)
    result = await run_in_threadpool(tools.run_tool, get_db(), current_user(request), tool_name,
                                     body.get("args") or {}, source="webmcp", confirmed=body.get("confirmed") is True)
    return JSONResponse(result)


# ── Command console (core/console.py) ─────────────────────────────


@app.get("/console", response_class=HTMLResponse)
async def console_page(request: Request):
    return templates.TemplateResponse(request, "console.html", {"active": "account"})


@app.post("/api/console")
async def api_console(request: Request):
    """Run one console line. Body: {"line": "...", "confirmed": bool}. Used by /console and `agency_os.py remote`."""
    if not request.state.via_key and request.headers.get("x-aos-console") != "1":
        return JSONResponse({"ok": False, "output": "Missing X-AOS-Console header"}, status_code=400)
    raw = await request.body()
    if len(raw) > _MAX_TOOL_BODY:
        return JSONResponse({"ok": False, "output": "Request too large"}, status_code=413)
    try:
        body = json.loads(raw or b"{}")
    except ValueError:
        body = None
    if not isinstance(body, dict) or not isinstance(body.get("line", ""), str):
        return JSONResponse({"ok": False, "output": "Body must be a JSON object with a \"line\""}, status_code=400)
    source = "cli" if request.state.via_key else "console"
    result = await run_in_threadpool(console.run_line, get_db(), current_user(request), body.get("line", ""),
                                     confirmed=body.get("confirmed") is True, source=source)
    return JSONResponse(result)


@app.post("/account/cli-keys")
async def account_cli_key_create(request: Request, name: str = Form(default="")):
    """Make a key for the remote CLI. It's shown once."""
    try:
        key = mcp_auth.create_cli_key(get_db(), current_user(request), name)
    except ValueError as exc:
        return _back("/account", error=str(exc))
    return _account(request, msg="CLI key created. Copy it now; it won't be shown again.", new_cli_key=key)


# ── Workflows: tutorials and the user's own replayable recipes (core/workflows.py) ─

WORKFLOW_EXAMPLE = """name: My morning check
description: Open the prospect list filtered to cold leads.
steps:
  - goto: /prospects?stage=cold
    say: These are today's cold leads.
  - highlight: table.data-table
    say: Start from the top.
  - pause: Call the first one, then log the call on their page.
"""


def _workflows_page(request: Request, *, msg: str = "", error: str = "", editor: str = "", status_code: int = 200):
    user = current_user(request)
    edit = request.query_params.get("edit", "")
    if not editor and edit:
        found = workflows.get_mine(get_db(), user, edit)
        if found:
            editor = yaml.safe_dump({k: found[k] for k in ("name", "description", "steps") if found.get(k)},
                                    sort_keys=False, allow_unicode=True, width=100)
    return templates.TemplateResponse(request, "workflows.html", {
        "active": "workflows",
        "tutorials": workflows.tutorials(user),
        "mine": workflows.mine(get_db(), user),
        "editor": editor or WORKFLOW_EXAMPLE,
        "msg": msg or request.query_params.get("msg", ""),
        "error": error or request.query_params.get("error", ""),
    }, status_code=status_code)


@app.get("/workflows", response_class=HTMLResponse)
async def workflows_page(request: Request):
    return _workflows_page(request)


@app.get("/api/workflows/{source}/{slug}")
async def workflow_definition(request: Request, source: str, slug: str):
    """A workflow for the player: a tutorial this user may see, or one of their own."""
    user = current_user(request)
    found = (workflows.tutorial(user, slug) if source == "tutorial"
             else workflows.get_mine(get_db(), user, slug) if source == "mine" else None)
    if found is None:
        return JSONResponse({"ok": False, "error": "Workflow not found"}, status_code=404)
    return {"ok": True, "workflow": {k: found[k] for k in ("name", "steps")}}


@app.post("/api/workflows/preview")
async def workflow_preview(request: Request):
    """Check an unsaved workflow from the editor and hand it to the player ("Try it")."""
    if request.headers.get("x-aos-workflow") != "1":
        return JSONResponse({"ok": False, "error": "Missing X-AOS-Workflow header"}, status_code=400)
    raw = await request.body()
    if len(raw) > MAX_WORKFLOW_UPLOAD:
        return JSONResponse({"ok": False, "error": "That workflow is too large"}, status_code=413)
    try:
        body = json.loads(raw or b"{}")
        wf = workflows.parse(body.get("definition", "") if isinstance(body, dict) else "")
    except ValueError as exc:  # WorkflowError, or a body that isn't JSON
        return JSONResponse({"ok": False, "error": str(exc) if isinstance(exc, workflows.WorkflowError)
                             else "Body must be JSON"}, status_code=400)
    return {"ok": True, "workflow": {k: wf[k] for k in ("name", "steps")}}


@app.post("/api/layouts/{page}")
async def layout_save(request: Request, page: str):
    """Save the user's own arrangement of a page's panels ({"layout": null} goes back to the defaults)."""
    if request.headers.get("x-aos-layout") != "1":
        return JSONResponse({"ok": False, "error": "Missing X-AOS-Layout header"}, status_code=400)
    raw = await request.body()
    if len(raw) > 64 * 1024:
        return JSONResponse({"ok": False, "error": "That layout is too large"}, status_code=413)
    try:
        body = json.loads(raw or b"{}")
        if not isinstance(body, dict) or "layout" not in body:
            raise panels.LayoutError('Send {"layout": {...}} or {"layout": null}')
        if page not in panels.PAGES:
            raise panels.LayoutError(f"No customizable page called {page!r}")
        panels.save(get_db(), current_user(request).id, page, body["layout"])
    except ValueError as exc:  # LayoutError, or a body that isn't JSON
        return JSONResponse({"ok": False, "error": str(exc) if isinstance(exc, panels.LayoutError)
                             else "Body must be JSON"}, status_code=400)
    return {"ok": True}


@app.post("/workflows/save")
async def workflow_save(request: Request, definition: str = Form(default="")):
    try:
        wf = workflows.parse(definition)
        slug = workflows.save(get_db(), current_user(request), wf)
    except workflows.WorkflowError as exc:
        return _workflows_page(request, error=str(exc), editor=definition, status_code=400)
    return RedirectResponse(url=f"/workflows?edit={quote(slug)}&msg={quote('Saved ' + wf['name'] + '.')}",
                            status_code=303)


@app.post("/workflows/import")
async def workflow_import(request: Request, file: UploadFile = File(...)):
    raw = await file.read(MAX_WORKFLOW_UPLOAD + 1)
    if len(raw) > MAX_WORKFLOW_UPLOAD:
        return _back("/workflows", error="That file is too large (1 MB max).")
    try:
        saved = workflows.import_text(get_db(), current_user(request), raw.decode("utf-8", errors="replace"))
    except workflows.WorkflowError as exc:
        return _back("/workflows", error=f"Nothing was imported. {exc}")
    return _back("/workflows", msg=f"Imported {len(saved)} workflow{'s' if len(saved) != 1 else ''}.")


@app.get("/workflows/export")
async def workflow_export(request: Request):
    backup = workflows.export(get_db(), current_user(request))
    name = f"agency-os-workflows-{datetime.now():%Y-%m-%d}.json"
    return Response(json.dumps(backup, indent=2), media_type="application/json",
                    headers={"Content-Disposition": f'attachment; filename="{name}"'})


@app.post("/workflows/{slug}/delete")
async def workflow_delete(request: Request, slug: str):
    if workflows.delete(get_db(), current_user(request), slug):
        return _back("/workflows", msg="Deleted.")
    return _back("/workflows", error="That workflow was not found.")


# ── Evidence webhooks (core/evidence.py): no login; signed or keyed ─


async def _webhook_body(request: Request) -> tuple[Optional[bytes], Optional[JSONResponse]]:
    body = await request.body()
    if len(body) > evidence.MAX_BODY:
        return None, JSONResponse({"ok": False, "error": "too large"}, status_code=413)
    return body, None


def _webhook_result(handler, body: bytes) -> JSONResponse:
    try:
        payload = evidence.parse(body)
    except ValueError:
        return JSONResponse({"ok": False, "error": "body must be JSON"}, status_code=400)
    return JSONResponse({"ok": True, "recorded": handler(get_db(), payload)})


@app.post("/webhooks/lob")
async def webhook_lob(request: Request):
    """Lob tracking events: returned mail becomes evidence for the lead guarantee."""
    if not os.environ.get("LOB_WEBHOOK_SECRET"):
        return JSONResponse({"detail": "Not Found"}, status_code=404)
    body, error = await _webhook_body(request)
    if error:
        return error
    if not evidence.lob_signature_ok(body, request.headers.get("lob-signature", ""),
                                     request.headers.get("lob-signature-timestamp", "")):
        return JSONResponse({"ok": False, "error": "bad signature"}, status_code=401)
    return await run_in_threadpool(_webhook_result, evidence.handle_lob, body)


@app.post("/webhooks/smartlead")
async def webhook_smartlead(request: Request, key: str = Query(default="")):
    """Smartlead EMAIL_BOUNCE events (Smartlead doesn't sign; the URL carries the key)."""
    if not os.environ.get("AGENCY_OS_WEBHOOK_KEY"):
        return JSONResponse({"detail": "Not Found"}, status_code=404)
    if not evidence.webhook_key_ok(key):
        return JSONResponse({"ok": False, "error": "bad key"}, status_code=401)
    body, error = await _webhook_body(request)
    return error or await run_in_threadpool(_webhook_result, evidence.handle_smartlead, body)


@app.post("/webhooks/bounce")
async def webhook_bounce(request: Request, key: str = Query(default="")):
    """Bounces from any other sender: {"email": "...", "type": "hard", "id": "..."}."""
    if not os.environ.get("AGENCY_OS_WEBHOOK_KEY"):
        return JSONResponse({"detail": "Not Found"}, status_code=404)
    if not evidence.webhook_key_ok(key):
        return JSONResponse({"ok": False, "error": "bad key"}, status_code=401)
    body, error = await _webhook_body(request)
    return error or await run_in_threadpool(_webhook_result, evidence.handle_generic, body)


# ── Browser calling (core/voice.py, docs/BROWSER_CALLING.md) ──────────


@app.get("/voice/token")
async def voice_token(request: Request):
    """A short-lived token for the browser's calling SDK. Super Admins only, for now."""
    found = voice.provider("twilio")
    if found is None:
        return JSONResponse({"detail": "Not Found"}, status_code=404)
    identity = voice.identity_for(current_user(request))
    return JSONResponse(
        {"token": found.access_token(identity, voice.TOKEN_TTL_SECONDS), "identity": identity,
         "expires_in": voice.TOKEN_TTL_SECONDS},
        headers={"Cache-Control": "no-store"},
    )


@app.post("/voice/calls/{voice_call_id}/disclosure")
async def voice_disclosure_read(request: Request, voice_call_id: int):
    """The rep read the recording disclosure (the call bar's "Disclosure read" button)."""
    if not voice.confirm_disclosure(get_db(), voice_call_id, current_user(request)):
        return JSONResponse({"detail": "Call not found"}, status_code=404)
    return JSONResponse({"ok": True})


@app.get("/voice/calls/{provider_key}/{call_sid}")
async def voice_call_status(request: Request, provider_key: str, call_sid: str):
    """The rep's own call, by the provider's call ID: the browser knows only that.
    Used for the Disclosure read button and to open the Log call form after hang-up."""
    call = get_db().get_voice_call_by_sid(provider_key, call_sid)
    if not call or call["user_id"] != current_user(request).id:
        return JSONResponse({"detail": "Call not found"}, status_code=404)
    return JSONResponse({"id": call["id"], "status": call["status"], "duration_seconds": call["duration_seconds"],
                         "outreach_id": call["outreach_id"], "prospect_id": call["prospect_id"],
                         "disclosure_read": bool(call["disclosure_read_at"])},
                        headers={"Cache-Control": "no-store"})


async def _voice_webhook(request: Request, provider_key: str):
    """(provider, form params) for a correctly signed provider webhook, or the error response."""
    found = voice.provider(provider_key)
    if found is None:
        return JSONResponse({"detail": "Not Found"}, status_code=404), None
    params = {name: str(value) for name, value in (await request.form()).items()}
    # Providers sign the public URL they called; behind Railway's proxy that's site_url().
    url = site_url() + request.url.path + (f"?{request.url.query}" if request.url.query else "")
    if not found.verify_webhook(url, params, dict(request.headers)):
        return JSONResponse({"ok": False, "error": "bad signature"}, status_code=401), None
    return found, params


@app.post("/webhooks/voice/{provider_key}/dial")
async def webhook_voice_dial(request: Request, provider_key: str):
    """The provider asks what to do with a call the browser started: dial the
    outreach's number from the campaign's caller ID, or tell the rep why not."""
    found, params = await _voice_webhook(request, provider_key)
    if params is None:
        return found
    ask = found.parse_dial(params)
    db = get_db()
    call, refusal = await run_in_threadpool(voice.place_call, db, get_campaigns(), found.key,
                                            ask["call_sid"], ask["identity"], ask["outreach_id"])
    user_id = voice.user_id_from_identity(ask["identity"])
    actor = db.load_current_user(user_id) if user_id else None
    if call is None:
        db.audit(actor, "voice.refused", "outreach", ask["outreach_id"][:20] or None,
                 {"provider": found.key, "reason": refusal})
        return Response(found.refuse_response(refusal), media_type=found.media_type)
    db.audit(actor, "voice.dial", "voice_call", call["id"],
             {"provider": found.key, "outreach_id": call["outreach_id"]})
    status_url = f"{site_url()}/webhooks/voice/{found.key}/status"
    return Response(found.dial_response(call["to_number"], call["caller_id"], status_url),
                    media_type=found.media_type)


@app.post("/webhooks/voice/{provider_key}/status")
async def webhook_voice_status(request: Request, provider_key: str):
    """How a call ended (completed, no-answer, busy...) and how long it lasted."""
    found, params = await _voice_webhook(request, provider_key)
    if params is None:
        return found
    await run_in_threadpool(voice.record_status, get_db(), found.key, found.parse_status(params))
    return Response(found.end_response(), media_type=found.media_type)


# ── Selling our own lists (core/selling.py) ─────────────────────────


def _x402_reply(reply: selling.Reply) -> JSONResponse:
    return JSONResponse(reply.body, status_code=reply.status, headers=reply.headers)


def _selling_off() -> Optional[JSONResponse]:
    return JSONResponse({"detail": "Not Found"}, status_code=404) if selling.problem() else None


@app.get("/x402/packages")
async def x402_catalog(request: Request):
    """Our free catalog for other agency-os instances (and any x402 lead buyer)."""
    off = _selling_off()
    return off or {"packages": await run_in_threadpool(selling.catalog, get_db())}


@app.get("/x402/packages/{slug}/leads")
async def x402_leads(request: Request, slug: str):
    off = _selling_off()
    if off:
        return off
    reply = await run_in_threadpool(selling.sell_leads, get_db(), selling.default_gate(), slug,
                                    request.headers.get("payment-signature", ""), str(request.url))
    return _x402_reply(reply)


@app.post("/x402/packages/{slug}/contacts")
async def x402_contacts(request: Request, slug: str):
    off = _selling_off()
    if off:
        return off
    try:
        body = evidence.parse(await request.body())
    except ValueError:
        return JSONResponse({"error": "body must be JSON"}, status_code=400)
    reply = await run_in_threadpool(selling.royalty, get_db(), selling.default_gate(), slug, body,
                                    request.headers.get("payment-signature", ""), str(request.url))
    return _x402_reply(reply)


@app.post("/x402/packages/{slug}/claims")
async def x402_claims(request: Request, slug: str):
    off = _selling_off()
    if off:
        return off
    try:
        body = evidence.parse(await request.body())
    except ValueError:
        return JSONResponse({"error": "body must be JSON"}, status_code=400)
    return _x402_reply(await run_in_threadpool(selling.handle_claim, get_db(), slug, body))


# ── Lead package generator (core/generator.py), Super Admins only ─────

_generator_tasks: set = set()  # keeps running generator tasks referenced until they finish


def _start_generator_run(run_id: int) -> None:
    """Run a queued generator run in a worker thread, so the request returns at once."""
    task = asyncio.create_task(asyncio.to_thread(generator.Runner(DB_URL or None).run, run_id))
    _generator_tasks.add(task)
    task.add_done_callback(_generator_tasks.discard)


def _generator_page(request: Request, *, msg: str = "", error: str = "", form: Optional[dict] = None):
    db = get_db()
    registry = _plugin_registry()
    enrichers = [{"key": k, "configured": bool(getattr(e, "is_configured", lambda: True)())}
                 for k, e in sorted(registry.enrichers.items())]
    return templates.TemplateResponse(request, "admin_generator.html", {
        "active": "admin", "campaigns": visible_campaigns(request), "enrichers": enrichers,
        "search_types": sorted(searches.plugins()), "runs": generator.recent(db), "max_leads": generator.MAX_LEADS,
        "fields": generator.FIELD_NAMES, "form": form or {}, "msg": msg, "error": error,
    })


@app.get("/admin/generator", response_class=HTMLResponse)
async def admin_generator(request: Request, msg: str = Query(default=""), error: str = Query(default="")):
    return _generator_page(request, msg=msg, error=error)


@app.post("/admin/generator", response_class=HTMLResponse)
async def admin_generator_create(request: Request):
    raw = await request.form()
    form = {k: str(v) for k, v in raw.items() if k != "enrichers"}
    chosen = [str(e) for e in raw.getlist("enrichers")]
    form["enrichers"] = chosen
    db = get_db()
    campaign = next((c for c in visible_campaigns(request) if c.db_name == form.get("campaign")), None)
    try:
        run = await run_in_threadpool(
            generator.create, db, current_user(request), title=form.get("title", ""),
            campaign_name=campaign.db_name if campaign else "",
            searches_wanted=generator.searches_from_form(form),
            enrichers=chosen if chosen else (campaign.enrichers if campaign else []),
            max_leads=form.get("max_leads"), known_enrichers=set(_plugin_registry().enrichers))
    except generator.GeneratorError as e:
        return _generator_page(request, error=str(e), form=form)
    _start_generator_run(run["id"])
    return RedirectResponse(url=f"/admin/generator/{run['id']}", status_code=303)


@app.get("/admin/generator/{run_id}", response_class=HTMLResponse)
async def admin_generator_run(request: Request, run_id: int, msg: str = Query(default="")):
    db = get_db()
    run = await run_in_threadpool(generator.get, db, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="No such run")
    publish_url = None
    if run["status"] == "done" and run["saved_list_id"] and run["eligible"]:
        publish_url = "/admin/selling?" + urlencode({"saved_list_id": run["saved_list_id"],
                                                     "guarantee_tier": generator.GUARANTEE_TIER,
                                                     "title": run["title"]})
    return templates.TemplateResponse(request, "admin_generator_run.html", {
        "active": "admin", "run": run, "publish_url": publish_url, "msg": msg,
        "prospects_url": "/prospects?" + urlencode({"generator_run": run_id}),
    })


@app.post("/admin/generator/{run_id}/cancel")
async def admin_generator_cancel(request: Request, run_id: int):
    run = await run_in_threadpool(generator.cancel, get_db(), current_user(request), run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="No such run")
    return RedirectResponse(url=f"/admin/generator/{run_id}?msg={quote('Cancel requested.')}", status_code=303)


# ── Customer accounts API (core/accounts.py, docs/U9ITUS_BILLING.md) ─────


def _bearer(request: Request) -> str:
    scheme, _, key = request.headers.get("authorization", "").partition(" ")
    return key.strip() if scheme.lower() == "bearer" else ""


def _api_error(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse({"error": code, "message": message}, status_code=status)


def _platform_refusal(request: Request) -> Optional[JSONResponse]:
    """None when the request carries u9itus's platform key."""
    if not accounts.platform_configured():
        return _api_error(503, "service_not_configured", "AGENCY_OS_PLATFORM_KEY_HASH is not set.")
    if not accounts.platform_key_ok(_bearer(request)):
        return _api_error(401, "unauthorized", "A valid platform key is required.")
    return None


def _account_or_refusal(request: Request) -> tuple[Optional[dict], Optional[JSONResponse]]:
    account = accounts.for_key(get_db(), _bearer(request))
    if account is None:
        return None, _api_error(401, "unauthorized", "A valid account key is required.")
    if account["status"] != "active":
        return None, _api_error(403, "account_suspended", "This account is suspended.")
    return account, None


async def _json_body(request: Request) -> dict:
    try:
        body = json.loads(await request.body() or b"{}")
    except ValueError:
        body = None
    if not isinstance(body, dict):
        raise accounts.AccountError("body must be a JSON object")
    return body


@app.post("/api/v1/accounts")
async def api_create_account(request: Request):
    """u9itus: make an account for a customer. 201 with its key; 200 (no key) if it exists."""
    refusal = _platform_refusal(request)
    if refusal:
        return refusal
    try:
        body = await _json_body(request)
        account, key = await run_in_threadpool(accounts.create, get_db(), str(body.get("external_ref") or ""),
                                               str(body.get("name") or ""))
    except accounts.AccountError as e:
        return _api_error(422, "invalid", str(e))
    reply = {"account": accounts.public(account)}
    if key:
        reply["key"] = key
    return JSONResponse(reply, status_code=201 if key else 200)


@app.post("/api/v1/accounts/{external_ref}/key")
async def api_rotate_account_key(request: Request, external_ref: str):
    """u9itus: issue a new key for an account; the old one stops working."""
    refusal = _platform_refusal(request)
    if refusal:
        return refusal
    try:
        key = await run_in_threadpool(accounts.rotate_key, get_db(), external_ref)
    except accounts.AccountError as e:
        return _api_error(404, "not_found", str(e))
    return {"key": key}


@app.post("/api/v1/accounts/{external_ref}/status")
async def api_account_status(request: Request, external_ref: str):
    """u9itus: suspend an account (e.g. a failed payment) or make it active again."""
    refusal = _platform_refusal(request)
    if refusal:
        return refusal
    try:
        body = await _json_body(request)
        account = await run_in_threadpool(accounts.set_status, get_db(), external_ref, str(body.get("status") or ""))
    except accounts.AccountError as e:
        status = 404 if "No such account" in str(e) else 422
        return _api_error(status, "not_found" if status == 404 else "invalid", str(e))
    return {"account": accounts.public(account)}


@app.get("/api/v1/account")
async def api_whoami(request: Request):
    account, refusal = await run_in_threadpool(_account_or_refusal, request)
    return refusal or {"account": accounts.public(account)}


@app.get("/api/v1/prospects")
async def api_account_prospects(request: Request, after: int = Query(default=0, ge=0),
                                limit: int = Query(default=50, ge=1, le=accounts.MAX_PAGE),
                                search: Optional[int] = Query(default=None)):
    """The prospects this account's searches found, in id order. Pass the last id as `after`;
    `search` limits it to what one search found."""
    account, refusal = await run_in_threadpool(_account_or_refusal, request)
    if refusal:
        return refusal
    db = get_db()
    if search is not None and await run_in_threadpool(searches.get, db, account["id"], search) is None:
        return _api_error(404, "not_found", "No such search.")
    rows = await run_in_threadpool(accounts.prospects, db, account["id"], after=after, limit=limit,
                                   search_id=search)
    return {"prospects": rows, "next_after": rows[-1]["id"] if len(rows) == limit else None}


@app.post("/api/v1/searches")
async def api_create_search(request: Request):
    """Queue a paid search. 202 when created; 200 for a retry with the same idempotency_key."""
    account, refusal = await run_in_threadpool(_account_or_refusal, request)
    if refusal:
        return refusal
    try:
        body = await _json_body(request)
        search, created = await run_in_threadpool(searches.create, get_db(), account["id"], body,
                                                  searches.plugins())
    except searches.SearchConflict as e:
        return _api_error(409, "conflict", str(e))
    except (searches.SearchError, accounts.AccountError) as e:
        return _api_error(422, "invalid", str(e))
    return JSONResponse({"search": searches.public(search)}, status_code=202 if created else 200)


@app.get("/api/v1/searches/{search_id}")
async def api_get_search(request: Request, search_id: int):
    account, refusal = await run_in_threadpool(_account_or_refusal, request)
    if refusal:
        return refusal
    search = await run_in_threadpool(searches.get, get_db(), account["id"], search_id)
    return {"search": searches.public(search)} if search else _api_error(404, "not_found", "No such search.")


@app.post("/api/v1/searches/{search_id}/cancel")
async def api_cancel_search(request: Request, search_id: int):
    """Cancel a search: a queued one ends now, a running one stops after the current page."""
    account, refusal = await run_in_threadpool(_account_or_refusal, request)
    if refusal:
        return refusal
    search = await run_in_threadpool(searches.cancel, get_db(), account["id"], search_id)
    return {"search": searches.public(search)} if search else _api_error(404, "not_found", "No such search.")


@app.get("/admin/selling", response_class=HTMLResponse)
async def admin_selling(request: Request, msg: str = Query(default=""), error: str = Query(default=""),
                        saved_list_id: str = Query(default=""), guarantee_tier: str = Query(default=""),
                        title: str = Query(default="")):
    # The lead package generator links here with its saved list chosen (core/generator.py).
    form = {"saved_list_id": saved_list_id, "guarantee_tier": guarantee_tier, "title": title[:200]}
    if guarantee_tier == generator.GUARANTEE_TIER:
        form["royalty_enriched"] = "0.02"
    return _selling_page(request, msg=msg, error=error, form={k: v for k, v in form.items() if v})


def _selling_page(request: Request, *, msg: str = "", error: str = "", preview: Optional[dict] = None,
                  form: Optional[dict] = None):
    db = get_db()
    return templates.TemplateResponse(request, "admin_selling.html", {
        "active": "admin", **selling.overview(db), "problem": selling.problem(),
        "network": selling.network_key(), "pay_to": selling.pay_to(), "catalog_url": f"{site_url()}/x402",
        "platform_fee_pct": selling.platform_fee_pct(), "max_pool_pct": selling.max_pool_pct(),
        "saved_lists": db.list_prospect_saved_lists(current_user(request).id),
        "tiers": [t for t in contact_depth.TIERS if t != "unworked"],
        "preview": preview, "form": form or {}, "msg": msg, "error": error,
    })


@app.post("/admin/selling/publish", response_class=HTMLResponse)
async def admin_selling_publish(request: Request):
    """Preview a saved list as a package, then publish it (two steps; publishing needs a confirm)."""
    form = {k: str(v) for k, v in (await request.form()).items()}
    db = get_db()
    try:
        saved_list_id = int(form.get("saved_list_id", ""))
    except ValueError:
        return _selling_page(request, error="Pick a saved list.", form=form)
    tier = form.get("guarantee_tier", "")
    saved = db.get_prospect_saved_list(saved_list_id)
    if saved is None or tier not in contact_depth.RANK:
        return _selling_page(request, error="Pick a saved list and a contact depth.", form=form)
    if form.get("action") != "publish" or form.get("confirm") != "yes":
        found = await run_in_threadpool(selling.preview, db, saved["criteria"], tier, hidden_for(request))
        return _selling_page(request, preview={**found, "eligible": len(found["eligible"]), "list": saved["name"]},
                             form=form, error="" if form.get("action") != "publish" else "Tick the box to publish.")
    package_id, problem = await run_in_threadpool(lambda: selling.publish(
        db, current_user(request), saved_list_id=saved_list_id, title=form.get("title", ""),
        industry=form.get("industry", ""), region=form.get("region", ""), unlock_usd=form.get("unlock_usd", ""),
        royalty_usd={t: form.get(f"royalty_{t}", "") for t in contact_depth.TIERS},
        guarantee_tier=tier, rules={"window_days": form.get("window_days", 30), "claim_days": form.get("claim_days", 7)},
        consent_note=form.get("consent_note", ""), sms_consent=form.get("sms_consent") == "1",
        contributor_pool_pct=form.get("contributor_pool_pct")))
    if problem:
        return _selling_page(request, error=problem, form=form)
    return _back("/admin/selling", msg="Package published.")


@app.post("/admin/selling/{package_id}/active")
async def admin_selling_active(request: Request, package_id: int, active: str = Form(default="")):
    selling.set_active(get_db(), current_user(request), package_id, active == "1")
    return _back("/admin/selling", msg="Package listed." if active == "1" else "Package withdrawn from the catalog.")


@app.post("/admin/selling/claims/{claim_id}/refund")
async def admin_selling_refund(request: Request, claim_id: int, amount_usd: str = Form(default=""),
                               tx_hash: str = Form(default="")):
    problem = selling.record_refund(get_db(), current_user(request), claim_id, amount_usd, tx_hash.strip())
    return _back("/admin/selling", error=problem) if problem else _back("/admin/selling", msg="Refund recorded.")


@app.post("/prospects/{prospect_id}/do-not-sell")
async def prospect_do_not_sell(request: Request, prospect_id: int, flag: str = Form(default="")):
    """Honor a request not to sell someone's data: never published, sold or used as a replacement."""
    selling.set_do_not_sell(get_db(), current_user(request), prospect_id, flag == "1")
    return _back(f"/prospects/{prospect_id}", msg="Marked do not sell." if flag == "1" else "Do-not-sell removed.")


@app.post("/prospects/{prospect_id}/do-not-call")
async def prospect_do_not_call(request: Request, prospect_id: int, flag: str = Form(default="")):
    """Honor a request not to be phoned or texted."""
    require_visible_prospect(request, prospect_id)
    get_db().set_do_not_call(current_user(request), prospect_id, flag == "1")
    return _back(f"/prospects/{prospect_id}", msg="Marked do not call." if flag == "1" else "Do-not-call removed.")


@app.post("/prospects/{prospect_id}/credit")
async def prospect_credit(request: Request, prospect_id: int, user_id: int = Form(...), task: str = Form(...),
                          active: str = Form(default="1")):
    """Give or remove a rep's credit for building this lead (their share of its sales)."""
    try:
        royalties.set_credit(get_db(), current_user(request), prospect_id, user_id, task, active == "1")
    except ValueError as exc:
        return _back(f"/prospects/{prospect_id}", error=str(exc))
    return _back(f"/prospects/{prospect_id}", msg="Credit updated.")


@app.get("/admin/payouts", response_class=HTMLResponse)
async def admin_payouts(request: Request, msg: str = Query(default=""), error: str = Query(default="")):
    db = get_db()
    return templates.TemplateResponse(request, "admin_payouts.html", {
        "active": "admin", "balances": royalties.balances(db), "payouts": royalties.payouts(db),
        "minimum": royalties.minimum(), "maximum": royalties.maximum(), "network": royalties.payout_network(),
        "automatic": royalties.default_sender().is_configured(), "msg": msg, "error": error,
    })


@app.post("/admin/payouts/{user_id}")
async def admin_payout(request: Request, user_id: int, confirm: str = Form(default=""), tx_hash: str = Form(default="")):
    """Pay a rep's balance: send it from the CDP wallet, or record a payment made by hand."""
    if not tx_hash.strip() and confirm != "yes":
        return _back("/admin/payouts", error="Tick the box to send the payment.")
    problem = await run_in_threadpool(
        royalties.pay, get_db(), current_user(request), user_id,
        sender=None if tx_hash.strip() else royalties.default_sender(), tx_hash=tx_hash.strip())
    return _back("/admin/payouts", error=problem) if problem else _back("/admin/payouts", msg="Payout done.")


@app.get("/plugins", response_class=HTMLResponse)
async def plugins_page(request: Request, msg: str = Query(default=""), error: str = Query(default=""),
                       test_slug: str = Query(default=""), test_demo: str = Query(default=""),
                       test_claim: str = Query(default="")):
    """Plugin management — view all plugins, their type, status, and config requirements."""
    from core.registry import PluginRegistry

    registry = PluginRegistry()
    registry.discover()

    # Build plugin info with descriptions and config status
    plugin_types = [
        ("prospect_sources", "Prospect Sources", "Where prospects come from"),
        ("products", "Products", "What you're selling"),
        ("channels", "Channels", "How outreach messages are delivered"),
        ("enrichers", "Enrichers", "Find contact names, emails, phones"),
        ("schedulers", "Schedulers", "Meeting booking services"),
    ]

    all_plugins = {}
    for ptype_key, ptype_label, ptype_desc in plugin_types:
        plugins_list = []
        keys = registry.list_plugins().get(ptype_key, [])
        for key in keys:
            if ptype_key == "prospect_sources":
                p = registry.get_source(key)
            elif ptype_key == "products":
                p = registry.get_product(key)
            elif ptype_key == "channels":
                p = registry.get_channel(key)
            elif ptype_key == "enrichers":
                p = registry.get_enricher(key)
            else:
                p = registry.get_scheduler(key)

            if p is None:
                continue

            # Get description from docstring
            desc = (p.__doc__ or "").strip().split("\n")[0] if p.__doc__ else ""

            # Get config status
            configured = p.is_configured() if hasattr(p, "is_configured") else True

            # Determine which env vars this plugin needs
            env_vars = _get_plugin_env_vars(key)

            # Count which campaigns use this plugin
            campaigns = get_campaigns()
            used_by = []
            for c in campaigns:
                if ptype_key == "prospect_sources" and key in c.prospect_sources:
                    used_by.append(c.name)
                elif ptype_key == "products" and key == c.product:
                    used_by.append(c.name)
                elif ptype_key == "channels" and key in c.channels:
                    used_by.append(c.name)
                elif ptype_key == "enrichers" and key in c.enrichers:
                    used_by.append(c.name)
                elif ptype_key == "schedulers" and key == c.scheduler:
                    used_by.append(c.name)

            plugins_list.append({
                "key": key,
                "portal_tests": ptype_key == "products" and portal_product(p) is not None
                                and hasattr(p, "check_connection") and hasattr(p, "create_test_portal"),
                "description": desc,
                "configured": configured,
                "env_vars": env_vars,
                "used_by": used_by,
            })
        all_plugins[ptype_key] = {
            "label": ptype_label,
            "description": ptype_desc,
            "plugins": plugins_list,
        }

    return templates.TemplateResponse(request, "plugins.html", {
        "active": "plugins",
        "all_plugins": all_plugins,
        "msg": msg,
        "error": error,
        "test_portal": {"slug": test_slug, "demo_url": test_demo, "claim_url": test_claim}
                       if test_slug else None,
    })


# ── Plugin pages (plugins/pages/, see core/plugin_pages.py) ────────


def _plugin_page(request: Request, page_key: str, posting: bool = False):
    """The page, or 404 if there's none; Forbidden unless its own permission allows this user."""
    page = plugin_pages.get(page_key)
    if page is None:
        raise HTTPException(status_code=404, detail="Page not found")
    user = current_user(request)
    if not (plugin_pages.can_post(user, page) if posting else plugin_pages.can_view(user, page)):
        raise Forbidden()
    return page


async def render_plugin_panels(request: Request, slot: str, prospect=None) -> list[dict]:
    """The plugin panels (core/plugin_panels.py) this user sees on a page, rendered."""
    if not plugin_panels.for_slot(slot):
        return []
    ctx = plugin_panels.PanelContext(
        user=current_user(request), db=get_db(), campaigns=visible_campaigns(request),
        hidden_campaigns=hidden_for(request), prospect=prospect)
    return await run_in_threadpool(plugin_panels.render, templates.env, slot, ctx)


def _plugin_page_context(request: Request) -> plugin_pages.PageContext:
    return plugin_pages.PageContext(
        user=current_user(request), db=get_db(), query=dict(request.query_params),
        campaigns=visible_campaigns(request), hidden_campaigns=hidden_for(request))


@app.get("/p/{page_key}", response_class=HTMLResponse)
async def plugin_page(request: Request, page_key: str, msg: str = Query(default=""), error: str = Query(default="")):
    page = _plugin_page(request, page_key)
    values = await run_in_threadpool(page.context, _plugin_page_context(request))
    return templates.TemplateResponse(request, plugin_pages.template_name(page), {
        **(values or {}),
        "plugin_page": page,
        "can_post": plugin_pages.can_post(current_user(request), page),
        "msg": msg,
        "error": error,
    })


@app.post("/p/{page_key}")
async def plugin_page_post(request: Request, page_key: str):
    page = _plugin_page(request, page_key, posting=True)
    form = {k: v for k, v in (await request.form()).items() if isinstance(v, str)}
    try:
        message = await run_in_threadpool(page.post, _plugin_page_context(request), form)
    except ValueError as exc:
        return _back(f"/p/{page_key}", error=str(exc) or "That didn't work.")
    return _back(f"/p/{page_key}", msg=message or "Done.")


@app.post("/plugins/{plugin_key}/test")
async def test_product_plugin(request: Request, plugin_key: str, action: str = Form(...)):
    """Exercise a demo-portal product's API from the Plugins page.

    "check" calls the product's check_connection() (no side effects) to confirm
    its URL and token. "create" calls create_test_portal(), which makes a blank
    demo page not tied to any prospect.
    """
    if action not in ("check", "create"):
        return _back("/plugins", error="Unknown action.")
    method = {"check": "check_connection", "create": "create_test_portal"}[action]
    product = portal_product(_plugin_registry().get_product(plugin_key))
    if product is None or not hasattr(product, method):
        return _back("/plugins", error=f"{plugin_key} has no test for that.")

    result = await asyncio.to_thread(getattr(product, method))
    if result.get("error"):
        status = result.get("status")
        detail = result.get("detail") or "request failed"
        return _back("/plugins", error=f"{plugin_key} test failed ({status or 'no response'}): {detail}")

    db = get_db()
    db.audit(current_user(request), f"plugin.{plugin_key}.{action}", "plugin", None,
             {"slug": result.get("slug")} if action == "create" else {})
    if action == "check":
        return _back("/plugins", msg=f"{plugin_key} connection OK: token accepted.")
    qs = urlencode({"msg": f"Test portal created ({result.get('status')}).",
                    "test_slug": result.get("slug") or "",
                    "test_demo": result.get("demo_url") or "",
                    "test_claim": result.get("claim_url") or ""})
    return RedirectResponse(url=f"/plugins?{qs}", status_code=303)


def _get_plugin_env_vars(plugin_key: str) -> list[str]:
    """Return the env var names a plugin needs to be configured."""
    env_map = {
        "email_smartlead": ["SMARTLEAD_API_KEY"],
        "email_smtp": ["SMTP_HOST", "SMTP_PORT", "SMTP_USER", "SMTP_PASS", "SMTP_FROM"],
        "sms_twilio": ["TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_FROM_NUMBER"],
        "lob_direct_mail": ["LOB_API_KEY", "LOB_FROM_NAME", "LOB_FROM_ADDRESS_LINE1", "LOB_FROM_ADDRESS_CITY", "LOB_FROM_ADDRESS_STATE", "LOB_FROM_ADDRESS_ZIP"],
        "apollo": ["APOLLO_API_KEY"],
        "hunter": ["HUNTER_API_KEY"],
        "calendly": ["CALENDLY_SCHEDULING_URL", "CALENDLY_API_TOKEN"],
        "u9itus_voter_guide": ["U9ITUS_BASE_URL", "U9ITUS_AGENCY_TOKEN"],
    }
    return env_map.get(plugin_key, [])


@app.get("/emails", response_class=HTMLResponse)
async def email_log(
    request: Request,
    campaign: str = Query(default=""),
    status: str = Query(default=""),
    page: int = Query(default=1, ge=1),
    per_page: int = Query(default=50, ge=10, le=200),
):
    """Email log viewer."""
    db = get_db()

    hidden = hidden_for(request)
    hidden_sql, params = db.hidden_clause(hidden, "o.prospect_id")
    where_parts = [f"(o.prospect_id IS NULL OR {hidden_sql})"]
    if campaign:
        where_parts.append("c.name = ?")
        params.append(campaign)
    if status:
        where_parts.append("e.status = ?")
        params.append(status)

    where_clause = " AND ".join(where_parts) if where_parts else "1=1"

    total = db.conn.execute(
        f"""SELECT COUNT(*) FROM email_log e
           JOIN campaigns c ON e.campaign_id = c.id
           LEFT JOIN outreach o ON e.outreach_id = o.id
           WHERE {where_clause}""",
        params,
    ).fetchone()[0]

    offset = (page - 1) * per_page
    rows = db.conn.execute(
        f"""SELECT e.*, c.name as campaign_name, p.name as prospect_name
           FROM email_log e
           JOIN campaigns c ON e.campaign_id = c.id
           LEFT JOIN outreach o ON e.outreach_id = o.id
           LEFT JOIN prospects p ON o.prospect_id = p.id
           WHERE {where_clause}
           ORDER BY e.sent_at DESC
           LIMIT ? OFFSET ?""",
        params + [per_page, offset],
    ).fetchall()

    campaigns_list = [r["name"] for r in db.conn.execute(
        "SELECT name FROM campaigns ORDER BY name"
    ).fetchall() if r["name"] not in hidden]

    total_pages = max(1, (total + per_page - 1) // per_page)

    return templates.TemplateResponse(request, "emails.html", {
        "emails": rows,
        "campaigns": campaigns_list,
        "campaign_filter": campaign,
        "status_filter": status,
        "page": page,
        "total": total,
        "total_pages": total_pages,
        "has_prev": page > 1,
        "has_next": page < total_pages,
    })


# ── Login & account ────────────────────────────────────────────────


def _safe_next(target: str) -> str:
    """Only allow local redirects after login (no //evil.com)."""
    if target.startswith("/") and not target.startswith("//") and target != "/":
        return target
    return ""


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, next: str = Query(default=""), error: str = Query(default="")):
    return templates.TemplateResponse(request, "login.html", {
        "next": next,
        "error": error,
        "no_users": get_db().count_users() == 0,
    })


@app.post("/login")
async def login(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    next: str = Form(default=""),
):
    client = request.client.host if request.client else "?"
    key = f"{email.strip().lower()}|{client}"
    account_key = f"{email.strip().lower()}|*"
    now = time.monotonic()
    for k in (key, account_key):
        _login_failures[k] = [t for t in _login_failures[k] if now - t < LOGIN_WINDOW_SECONDS]

    def fail(message: str):
        return RedirectResponse(
            url=f"/login?error={quote(message)}&next={quote(next)}", status_code=303
        )

    if (len(_login_failures[key]) >= LOGIN_MAX_FAILURES
            or len(_login_failures[account_key]) >= LOGIN_ACCOUNT_MAX_FAILURES):
        return fail("Too many failed attempts. Try again in 15 minutes.")

    db = get_db()
    row = db.get_user_by_email(email)
    # Always run a hash check so response time doesn't reveal which emails exist
    ok = access.verify_password(password, row["password_hash"] if row else _DUMMY_HASH)
    if not (row and ok and row["is_active"]):
        _login_failures[key].append(now)
        _login_failures[account_key].append(now)
        return fail("Incorrect email or password.")

    _login_failures.pop(key, None)
    _login_failures.pop(account_key, None)
    user = db.load_current_user(row["id"])
    db.audit(user, "auth.login", "user", user.id)
    return _start_session(request, user, _safe_next(next) or user.landing_page())


def _start_session(request: Request, user: CurrentUser, target: str) -> RedirectResponse:
    token = access.new_session_token()
    get_db().create_session(user.id, access.hash_token(token), SESSION_TTL_DAYS)
    response = RedirectResponse(url=target, status_code=303)
    response.set_cookie(
        SESSION_COOKIE, token,
        max_age=SESSION_TTL_DAYS * 86400,
        httponly=True,
        samesite="lax",
        secure=request.url.scheme == "https",
    )
    return response


def _set_password_page(request: Request, token: str, error: str, reset: bool):
    """The page behind a one-time link: welcome (new user) or password reset."""
    db = get_db()
    user_id = db.invite_user_id(access.hash_token(token))
    invitee = db.load_current_user(user_id) if user_id else None
    return templates.TemplateResponse(request, "welcome.html", {
        "invitee": invitee,
        "error": error,
        "reset": reset,
    }, status_code=200 if invitee else 410)


def _accept_set_password(request: Request, token: str, new_password: str,
                         confirm_password: str, reset: bool):
    page = f"/{'reset-password' if reset else 'welcome'}/{quote(token)}"
    if new_password != confirm_password:
        return RedirectResponse(url=f"{page}?error=Passwords+don%27t+match.", status_code=303)
    db = get_db()
    try:
        user_id = db.accept_invite(access.hash_token(token), new_password)
    except AccessError as e:
        return RedirectResponse(url=f"{page}?error={quote(str(e))}", status_code=303)
    user = db.load_current_user(user_id)
    db.audit(user, "auth.password_reset" if reset else "auth.invite_accepted", "user", user.id)
    return _start_session(request, user, user.landing_page())


@app.get("/welcome/{token}", response_class=HTMLResponse)
async def welcome_page(request: Request, token: str, error: str = Query(default="")):
    """Landing page for the one-time link in a welcome email (core/welcome.py)."""
    return _set_password_page(request, token, error, reset=False)


@app.post("/welcome/{token}")
async def accept_welcome(
    request: Request,
    token: str,
    new_password: str = Form(...),
    confirm_password: str = Form(...),
):
    return _accept_set_password(request, token, new_password, confirm_password, reset=False)


# ── Unsubscribe (core/compliance.py): no login; the link's token authorizes ─


@app.get("/unsubscribe", response_class=HTMLResponse)
async def unsubscribe_page(request: Request, e: str = Query(default=""), t: str = Query(default="")):
    """Asks before unsubscribing, so link scanners that open every URL don't opt people out."""
    ok = compliance.token_ok(e, t)
    return templates.TemplateResponse(request, "unsubscribe.html", {
        "email": compliance.normalize(e), "token": t, "valid": ok, "done": False,
    }, status_code=200 if ok else 400)


@app.post("/unsubscribe", response_class=HTMLResponse)
async def unsubscribe(request: Request, e: str = Query(default=""), t: str = Query(default="")):
    """The page's button and mail clients' one-click List-Unsubscribe POST both land here."""
    if not compliance.token_ok(e, t):
        return templates.TemplateResponse(request, "unsubscribe.html", {
            "email": "", "token": "", "valid": False, "done": False,
        }, status_code=400)
    await run_in_threadpool(get_db().suppress_email, e, "unsubscribed", "link")
    return templates.TemplateResponse(request, "unsubscribe.html", {
        "email": compliance.normalize(e), "token": "", "valid": True, "done": True,
    })


@app.get("/forgot-password", response_class=HTMLResponse)
async def forgot_password_page(request: Request, sent: bool = Query(default=False),
                               error: str = Query(default="")):
    return templates.TemplateResponse(request, "forgot_password.html", {
        "sent": sent,
        "error": error,
        "smtp_ready": welcome_email.smtp_configured(),
    })


def _send_reset_email(email: str) -> None:
    """Runs after the response is sent, so timing never reveals whether the email exists."""
    db = get_db()
    row = db.get_user_by_email(email)
    if not (row and row["is_active"]):
        return
    link, _ = welcome_email.issue_reset(db, row["id"], site_url())
    user = db.load_current_user(row["id"])
    subject, body = welcome_email.compose_reset(user, link, site_url())
    result = welcome_email.send(user.email, subject, body)
    if result.status != "sent":
        print(f"[password reset] email to {user.email} not sent: {result.error}", file=sys.stderr)


@app.post("/forgot-password")
async def forgot_password(request: Request, background: BackgroundTasks, email: str = Form(...)):
    """Email a one-time reset link. Answers the same whether or not the account exists."""
    email = email.strip().lower()
    client = request.client.host if request.client else "?"
    now = time.monotonic()
    keys = (f"email|{email}", f"ip|{client}")
    for key in keys:
        _reset_requests[key] = [t for t in _reset_requests[key] if now - t < LOGIN_WINDOW_SECONDS]
    if any(len(_reset_requests[key]) >= RESET_MAX_REQUESTS for key in keys):
        return RedirectResponse(
            url="/forgot-password?error=Too+many+requests.+Try+again+in+15+minutes.", status_code=303
        )
    for key in keys:
        _reset_requests[key].append(now)

    if welcome_email.smtp_configured():
        background.add_task(_send_reset_email, email)
    return RedirectResponse(url="/forgot-password?sent=1", status_code=303)


@app.get("/reset-password/{token}", response_class=HTMLResponse)
async def reset_password_page(request: Request, token: str, error: str = Query(default="")):
    """Landing page for the one-time link in a password-reset email."""
    return _set_password_page(request, token, error, reset=True)


@app.post("/reset-password/{token}")
async def reset_password(
    request: Request,
    token: str,
    new_password: str = Form(...),
    confirm_password: str = Form(...),
):
    return _accept_set_password(request, token, new_password, confirm_password, reset=True)


@app.post("/logout")
async def logout(request: Request):
    token = request.cookies.get(SESSION_COOKIE)
    if token:
        get_db().delete_session(access.hash_token(token))
    response = RedirectResponse(url="/login", status_code=303)
    response.delete_cookie(SESSION_COOKIE)
    return response


@app.get("/account", response_class=HTMLResponse)
async def account_page(request: Request, msg: str = Query(default=""), error: str = Query(default="")):
    return _account(request, msg=msg, error=error)


def _account(request: Request, *, msg: str = "", error: str = "", new_token: str = "", new_cli_key: str = ""):
    user = current_user(request)
    cli = None
    if user.can("cli.use"):
        cli = {"saved_keys": mcp_auth.list_cli_keys(get_db(), user.id), "new_key": new_cli_key, "url": site_url()}
    spending = None
    if user.can("packages.buy"):
        db = get_db()
        spending = {**db.user_spend(user.id), "allowance_atomic": db.spend_allowance(user.id)}
    ai = None
    if user.can("agents.use") or user.can("ai.connect"):
        ai = {"enabled": user.ai_enabled, "allowed": access.ai_allowed(), "model": llm.describe(),
              "mcp_url": f"{site_url()}/mcp",
              "tokens": mcp_auth.list_tokens(get_db(), user.id) if user.uses_ai("ai.connect") else [],
              "new_token": new_token}
    earnings = None
    if user.can("royalties.view_own"):
        db = get_db()
        mine = royalties.balances(db, user.id)
        prefs = db.conn.execute("SELECT payout_address FROM user_prefs WHERE user_id = ?", (user.id,)).fetchone()
        earnings = {"balance": mine[0] if mine else {"pending": 0, "payable": 0, "paid": 0},
                    "lines": royalties.statement(db, user.id, 20), "labels": royalties.TASK_LABELS,
                    "address": prefs["payout_address"] if prefs else None}
    return templates.TemplateResponse(request, "account.html", {
        "active": "account",
        "spending": spending,
        "earnings": earnings,
        "ai": ai,
        "cli": cli,
        "permissions": [(k, v) for k, v in access.CATALOG.items() if user.can(k)],
        "msg": msg,
        "error": error,
    })


@app.post("/account/ai")
async def account_ai(request: Request, enabled: str = Form(default="")):
    """Turn AI features on or off for yourself. Off keeps the app exactly as before."""
    user = current_user(request)
    if not (user.can("agents.use") or user.can("ai.connect")):
        return _back("/account", error="Your role doesn't include AI features. Ask an owner.")
    get_db().set_ai_enabled(user.id, enabled == "1", actor=user)
    return _back("/account", msg="AI features turned on." if enabled == "1" else "AI features turned off.")


@app.post("/account/tokens")
async def account_token_create(request: Request, name: str = Form(default=""), allow_writes: str = Form(default="")):
    """Make a personal token for an MCP client. It's shown once."""
    user = current_user(request)
    if not user.uses_ai("ai.connect"):
        return _back("/account", error="Turn on AI features first.")
    try:
        token = mcp_auth.create_personal_token(get_db(), user, name, allow_writes == "1")
    except ValueError as exc:
        return _back("/account", error=str(exc))
    # Rendered straight into this response (never put in a URL), and never shown again.
    return _account(request, msg="Access key created. Copy it now; it won't be shown again.", new_token=token)


@app.post("/account/tokens/{token_id}/revoke")
async def account_token_revoke(request: Request, token_id: int):
    revoked = mcp_auth.revoke(get_db(), current_user(request), token_id)
    return _back("/account", msg="Disconnected. That app or key can no longer reach agency-os.") if revoked else _back("/account", error="That connection was not found.")


@app.get("/oauth/consent", response_class=HTMLResponse)
async def oauth_consent(request: Request, request_id: str = Query(default="", alias="request")):
    """An app (Claude.ai, ChatGPT...) asks to act as you in agency-os."""
    user = current_user(request)
    found = mcp_auth.load_request(get_db(), request_id) if request_id else None
    client, params = found if found else (None, None)
    return templates.TemplateResponse(request, "oauth_consent.html", {
        "client": client, "params": params, "request_id": request_id,
        "ai_on": user.uses_ai("ai.connect"),
        "redirect_host": urlsplit(str(params.redirect_uri)).netloc if params else "",
    })


@app.post("/oauth/consent")
async def oauth_consent_decide(request: Request, request_id: str = Form(..., alias="request"),
                               decision: str = Form(...), allow_writes: str = Form(default="")):
    user = current_user(request)
    if decision == "approve" and not user.uses_ai("ai.connect"):
        return _back("/account", error="Turn on AI features first, then connect the app again.")
    target = mcp_auth.decide(get_db(), user, request_id, decision == "approve", allow_writes == "1")
    if target is None:
        return _back("/account", error="That sign-in request expired. Start the connection again from the app.")
    return RedirectResponse(url=target, status_code=303)


@app.post("/account/payout-address")
async def account_payout_address(request: Request, address: str = Form(default=""),
                                 current_password: str = Form(...)):
    """Where your data royalties are paid. Needs your password, since it redirects money."""
    user = current_user(request)
    db = get_db()
    row = db.get_user_by_email(user.email)
    if not access.verify_password(current_password, row["password_hash"]):
        return _back("/account", error="Current password is incorrect.")
    problem = royalties.set_payout_address(db, user, address)
    return _back("/account", error=problem) if problem else _back("/account", msg="Payout address saved.")


@app.post("/account/password")
async def change_password(
    request: Request,
    current_password: str = Form(...),
    new_password: str = Form(...),
    confirm_password: str = Form(...),
):
    user = current_user(request)
    db = get_db()
    row = db.get_user_by_email(user.email)
    if not access.verify_password(current_password, row["password_hash"]):
        return RedirectResponse(url="/account?error=Current+password+is+incorrect.", status_code=303)
    if new_password != confirm_password:
        return RedirectResponse(url="/account?error=New+passwords+don%27t+match.", status_code=303)
    try:
        token = request.cookies.get(SESSION_COOKIE, "")
        db.set_password(user.id, new_password, user, keep_session_hash=access.hash_token(token))
    except AccessError as e:
        return RedirectResponse(url=f"/account?error={quote(str(e))}", status_code=303)
    return RedirectResponse(
        url="/account?msg=Password+changed.+Other+sessions+were+signed+out.", status_code=303
    )


# ── Team administration (owners only) ──────────────────────────────


def _accounts_page(request: Request, *, msg: str = "", error: str = "", new_key: Optional[dict] = None):
    db = get_db()
    usage = accounts.recent_usage(db)
    rows = [{**a, **usage.get(a["id"], {"searches": 0, "delivered": 0})} for a in accounts.list_accounts(db)]
    return templates.TemplateResponse(request, "admin_accounts.html", {
        "active": "admin", "tab": "accounts", "accounts": rows, "new_key": new_key,
        "platform_configured": accounts.platform_configured(), "msg": msg, "error": error,
    })


@app.get("/admin/accounts", response_class=HTMLResponse)
async def admin_accounts(request: Request, msg: str = Query(default=""), error: str = Query(default="")):
    """Customer accounts u9itus made (docs/U9ITUS_BILLING.md, task B8)."""
    return _accounts_page(request, msg=msg, error=error)


@app.post("/admin/accounts/{external_ref}/status")
async def admin_account_status(request: Request, external_ref: str, status: str = Form(default="")):
    try:
        accounts.set_status(get_db(), external_ref, status, actor=current_user(request))
    except accounts.AccountError as e:
        return _back("/admin/accounts", error=str(e))
    return _back("/admin/accounts", msg=f"{external_ref} is now {status}.")


@app.post("/admin/accounts/{external_ref}/key", response_class=HTMLResponse)
async def admin_account_key(request: Request, external_ref: str):
    """Issue a new key. It's shown once, on this response only (never in a URL or a redirect)."""
    try:
        key = accounts.rotate_key(get_db(), external_ref, actor=current_user(request))
    except accounts.AccountError as e:
        return _back("/admin/accounts", error=str(e))
    response = _accounts_page(request, msg=f"New key issued for {external_ref}. The old key no longer works.",
                              new_key={"external_ref": external_ref, "key": key})
    response.headers["Cache-Control"] = "no-store"
    return response



def _back(path: str, *, msg: str = "", error: str = "") -> RedirectResponse:
    qs = f"?msg={quote(msg)}" if msg else f"?error={quote(error)}" if error else ""
    return RedirectResponse(url=f"{path}{qs}", status_code=303)


@app.get("/admin/users", response_class=HTMLResponse)
async def admin_users(request: Request, msg: str = Query(default=""), error: str = Query(default="")):
    db = get_db()
    me = current_user(request)
    roles = db.list_roles()
    protected = {r["id"] for r in roles if r["is_protected"]}
    users = db.list_users()
    for u in users:
        # Owner and Super Admin accounts other than your own are a Super Admin's to change.
        u["locked"] = not me.is_super_admin and u["id"] != me.id and bool(u["role_ids"] & protected)
        u["agent_seen_label"] = access.describe_channel(u["agent_seen_via"] or "")
        u["agent_inbox"] = access.looks_like_agent_email(u["email"])
    return templates.TemplateResponse(request, "admin_users.html", {
        "active": "admin",
        "users": users,
        "roles": roles,
        "protected_role_ids": protected,
        "msg": msg,
        "error": error,
    })


@app.post("/admin/users")
async def admin_create_user(
    request: Request,
    email: str = Form(...),
    name: str = Form(...),
    password: str = Form(...),
    role_ids: list[int] = Form(default=[]),
    is_agent: str = Form(default=""),
):
    try:
        get_db().create_user(email, name, password, role_ids, current_user(request), is_agent=bool(is_agent))
    except AccessError as e:
        return _back("/admin/users", error=str(e))
    return _back("/admin/users", msg=f"Added {email.strip().lower()}.")


@app.post("/admin/users/{user_id}")
async def admin_update_user(
    request: Request,
    user_id: int,
    name: str = Form(...),
    is_active: str = Form(default=""),
    is_agent: str = Form(default=""),
    role_ids: list[int] = Form(default=[]),
    new_password: str = Form(default=""),
):
    actor = current_user(request)
    db = get_db()
    try:
        db.update_user(user_id, name=name, is_active=bool(is_active), role_ids=role_ids, actor=actor,
                       is_agent=bool(is_agent))
        if new_password:
            db.set_password(user_id, new_password, actor)
    except AccessError as e:
        return _back("/admin/users", error=str(e))
    return _back("/admin/users", msg=f"Saved {name.strip()}.")


@app.get("/admin/roles", response_class=HTMLResponse)
async def admin_roles(request: Request, msg: str = Query(default=""), error: str = Query(default="")):
    return templates.TemplateResponse(request, "admin_roles.html", {
        "active": "admin",
        "roles": get_db().list_roles(),
        "catalog": access.CATALOG,
        "msg": msg,
        "error": error,
    })


@app.post("/admin/roles")
async def admin_create_role(
    request: Request,
    name: str = Form(...),
    description: str = Form(default=""),
    permissions: list[str] = Form(default=[]),
):
    try:
        get_db().create_role(name, description, permissions, current_user(request))
    except AccessError as e:
        return _back("/admin/roles", error=str(e))
    return _back("/admin/roles", msg=f"Created role {name.strip()}.")


@app.post("/admin/roles/{role_id}")
async def admin_update_role(
    request: Request,
    role_id: int,
    name: str = Form(...),
    description: str = Form(default=""),
    permissions: list[str] = Form(default=[]),
):
    try:
        get_db().update_role(role_id, name, description, permissions, current_user(request))
    except AccessError as e:
        return _back("/admin/roles", error=str(e))
    return _back("/admin/roles", msg=f"Saved role {name.strip()}.")


@app.post("/admin/roles/{role_id}/delete")
async def admin_delete_role(request: Request, role_id: int):
    try:
        get_db().delete_role(role_id, current_user(request))
    except AccessError as e:
        return _back("/admin/roles", error=str(e))
    return _back("/admin/roles", msg="Role deleted.")


@app.get("/admin/audit", response_class=HTMLResponse)
async def admin_audit(request: Request):
    return templates.TemplateResponse(request, "admin_audit.html", {
        "active": "admin",
        "entries": _visible_audit(request, get_db().list_audit()),
    })


def _visible_audit(request: Request, entries: list) -> list:
    """Leave out entries about campaigns (or their prospects) this user can't see."""
    hidden = hidden_for(request)
    if not hidden:
        return entries
    db = get_db()
    kept = []
    for e in entries:
        if e["target_type"] == "campaign" and e["target_id"] in hidden:
            continue
        if any(name in (e["details"] or "") for name in hidden):
            continue
        if e["target_type"] == "prospect" and str(e["target_id"] or "").isdigit() \
                and db.prospect_hidden(int(e["target_id"]), hidden):
            continue
        kept.append(e)
    return kept


@app.get("/admin/jobs", response_class=HTMLResponse)
async def admin_jobs(request: Request, msg: str = Query(default=""), error: str = Query(default="")):
    runner = get_job_runner()
    last = runner.last_runs()
    due = {job.key for job in runner.due_jobs()}
    registry = _plugin_registry()
    portal_products = {p.key: p for c in get_campaigns()
                       if (p := portal_product(registry.get_product(c.product)))}
    return templates.TemplateResponse(request, "admin_jobs.html", {
        "active": "admin",
        "jobs": [{"job": job, "last": last.get(job.key), "due": job.key in due} for job in configured_jobs()],
        "runs": runner.recent_runs(),
        "enabled": jobs_enabled(),
        "unconfigured_portals": [p for p in portal_products.values() if not p.is_configured()],
        "msg": msg,
        "error": error,
    })


@app.post("/admin/jobs/{job_key}/run")
async def admin_run_job(request: Request, job_key: str):
    if job_key not in {job.key for job in configured_jobs()}:
        return _back("/admin/jobs", error="Unknown job.")
    result = await asyncio.to_thread(get_job_runner().run, job_key, "manual")
    get_db().audit(current_user(request), "job.run", "job", job_key, {"ok": result["ok"]})
    if result["ok"]:
        return _back("/admin/jobs", msg=f"Ran {job_key}.")
    return _back("/admin/jobs", error=f"{job_key} finished with a problem. See the latest run below.")


# ── Campaign management (owners only) ───────────────────────────────


def _get_registry_plugins():
    """Return all plugins grouped by type, with metadata."""
    from core.registry import PluginRegistry
    registry = PluginRegistry()
    registry.discover()

    result = {}
    for ptype in ["prospect_sources", "products", "channels", "enrichers", "schedulers"]:
        plugins_list = []
        for key in registry.list_plugins().get(ptype, []):
            if ptype == "prospect_sources":
                p = registry.get_source(key)
            elif ptype == "products":
                p = registry.get_product(key)
            elif ptype == "channels":
                p = registry.get_channel(key)
            elif ptype == "enrichers":
                p = registry.get_enricher(key)
            else:
                p = registry.get_scheduler(key)
            if p is None:
                continue
            desc = (p.__doc__ or "").strip().split("\n")[0] if p.__doc__ else ""
            configured = p.is_configured() if hasattr(p, "is_configured") else True
            plugins_list.append({
                "key": key,
                "description": desc,
                "configured": configured,
            })
        result[ptype] = plugins_list
    return result


@app.get("/admin/campaigns", response_class=HTMLResponse)
async def admin_campaigns(request: Request, msg: str = Query(default=""), error: str = Query(default="")):
    """Admin campaign management — list campaigns with YAML viewer and plugin association."""
    campaigns = visible_campaigns(request)
    db = get_db()

    campaign_data = []
    members_by_campaign: dict[str, list] = defaultdict(list)
    for m in db.campaign_members():
        members_by_campaign[m["campaign"]].append(m)
    owners_by_campaign: dict[str, list] = defaultdict(list)
    for o in db.campaign_owners():
        owners_by_campaign[o["campaign"]].append(o)
    for c in campaigns:
        stats = db.get_pipeline_stats(c.db_name)
        # Read the raw YAML for display
        yaml_path = c.config_dir / "campaign.yaml"
        yaml_content = yaml_path.read_text() if yaml_path.exists() else ""
        campaign_data.append({
            "config": c,
            "stats": stats,
            "yaml": yaml_content,
            "members": [m["name"] for m in members_by_campaign.get(c.db_name, [])],
            "owners": [o["name"] for o in owners_by_campaign.get(c.db_name, [])],
        })

    # Get available plugins for the create-campaign form
    plugins = _get_registry_plugins()

    return templates.TemplateResponse(request, "admin_campaigns.html", {
        "active": "admin",
        "campaigns": campaign_data,
        "plugins": plugins,
        "msg": msg,
        "error": error,
    })


@app.post("/admin/campaigns/create")
async def admin_create_campaign(
    request: Request,
    name: str = Form(...),
    product: str = Form(...),
    prospect_sources: list[str] = Form(default=[]),
    channels: list[str] = Form(default=[]),
    enrichers: list[str] = Form(default=[]),
    scheduler: str = Form(default=""),
    sender_name: str = Form(default=""),
    sender_email: str = Form(default=""),
    state: str = Form(default=""),
    county: str = Form(default=""),
    min_revenue: str = Form(default=""),
    stale_threshold_days: str = Form(default="14"),
    touch_count: str = Form(default="4"),
    cadence_days: str = Form(default="3,4,5"),
):
    """Create a new campaign with a campaign.yaml and starter scripts.

    Generates a folder under campaigns/<slug>/ with:
      - campaign.yaml (full config with plugins, filters, cadence, stages)
      - scripts/00_cold_outreach.yaml (starter email script)
      - scripts/phone_00_cold_call.yaml (starter phone script)

    The admin can then edit plugins, import CSV leads, and customize scripts
    from the campaign detail page.
    """
    if not _same_origin(request):
        return _back("/admin/campaigns", error="Cross-site request blocked.")

    import re
    import yaml as _yaml

    # Generate slug from name
    slug = re.sub(r"[^a-z0-9-]", "", name.lower().replace(" ", "-"))
    if not slug:
        return _back("/admin/campaigns", error="Campaign name must produce a valid slug.")

    # Check for duplicate
    existing = get_campaigns()
    for c in existing:
        if c.db_name == slug:
            return _back("/admin/campaigns", error=f"Campaign '{slug}' already exists.")

    if not prospect_sources:
        return _back("/admin/campaigns", error="Select at least one prospect source.")
    if not channels:
        return _back("/admin/campaigns", error="Select at least one channel.")
    problem = access.channel_change_problem(current_user(request), [], channels)
    if problem:
        return _back("/admin/campaigns", error=problem)

    # Build filters
    filters = {}
    if state:
        filters["state"] = state.upper()
    if county:
        filters["county"] = county
    if min_revenue:
        try:
            filters["min_revenue"] = int(min_revenue)
        except ValueError:
            pass

    # Build cadence
    try:
        n_touches = max(1, int(touch_count))
    except ValueError:
        n_touches = 4

    delay_parts = cadence_days.split(",") if cadence_days else []
    delays = []
    for d in delay_parts:
        d = d.strip()
        if d:
            try:
                delays.append(int(d))
            except ValueError:
                pass
    while len(delays) < n_touches:
        delays.append(3)

    stages = ["cold", "contacted", "engaged", "demo_scheduled", "proposal_sent", "closed_won", "closed_lost", "nurture"]

    script_names = ["00_cold_outreach", "01_followup_impact", "02_followup_cobrand", "03_breakup",
                    "04_followup_2", "05_followup_3", "06_followup_4", "07_breakup_2"]
    next_stages = ["contacted", "contacted", "contacted", "nurture",
                   "contacted", "contacted", "contacted", "nurture"]

    cadence = []
    cumulative = 0
    for i in range(n_touches):
        cumulative += delays[i] if i < len(delays) else 3
        cadence.append({
            "touch": i,
            "delay_days": cumulative if i > 0 else 0,
            "script": script_names[i] if i < len(script_names) else f"0{i}_followup",
            "next_stage": next_stages[i] if i < len(next_stages) else "contacted",
        })

    # Build the campaign YAML
    raw = {
        "name": name,
        "product": product,
        "prospect_sources": prospect_sources,
        "channels": channels,
        "enrichers": enrichers if enrichers else [],
    }
    if scheduler:
        raw["scheduler"] = scheduler
    if filters:
        raw["filters"] = filters
    raw["stages"] = stages
    raw["cadence"] = cadence
    raw["stale_threshold_days"] = int(stale_threshold_days) if stale_threshold_days else 14
    if sender_name:
        raw["sender_name"] = sender_name
    if sender_email:
        raw["sender_email"] = sender_email

    yaml_content = _yaml.dump(raw, default_flow_style=False, sort_keys=False, allow_unicode=True)

    # Starter email script
    cold_script = {
        "key": f"{slug}_cold_outreach",
        "stage": "cold",
        "subject": "Voters in {{city}} deserve better candidate info",
        "body": (
            "Hi {{contact_first}},\n\n"
            "68% of voters say they lack confidence in their candidate research.\n"
            "That's not a voter problem — it's an information problem.\n\n"
            "We built U9itus: one platform, verified public records, no account needed.\n"
            "{{org_name}} can distribute a personalized, nonpartisan voter guide to your\n"
            "constituents — candidates side-by-side, ballot measures in plain language,\n"
            "co-branded with your logo, embeddable on your site.\n\n"
            "Interested in learning how this works for your district?\n\n"
            "{{your_name}}"
        ),
    }

    # Starter phone script
    phone_script = {
        "key": f"{slug}_cold_call",
        "stage": "cold",
        "goal": "Get past the gatekeeper and schedule a 15-min demo",
        "script": (
            "Hi, I'm calling for {{contact_first}}. [Wait]\n\n"
            "Hi {{contact_first}}, I'm {{your_name}} with U9itus. We build digital voter\n"
            "guides that organizations like {{org_name}} can share with their community.\n\n"
            "I'll keep this brief — can I send you a link to a personalized demo so you\n"
            "can see what it looks like for your district?\n\n"
            "[If yes]: Great, what's the best email? [Send demo link]\n"
            "[If no]: No problem. Is there someone else I should talk to about\n"
            "civic engagement or voter education initiatives?\n\n"
            "[If gatekeeper]: I'm following up about a voter education resource for\n"
            "{{org_name}}. What's the best way to reach {{contact_first}} or whoever\n"
            "handles community programs?"
        ),
        "objections": {
            "Not interested": "Totally understand. Can I at least send a one-page overview so you have it on file?",
            "Too busy": "I respect your time — this takes 15 seconds to review. I'll send a link and follow up in a week.",
            "We already have a voter guide": "That's great — ours is complementary, not a replacement. Can I show you how it's different?",
            "Send me an email": "Absolutely — what's the best address? [Send demo link, schedule follow-up call]",
        },
    }

    # Save all files to the database and local cache
    db = get_db()
    campaign_rel_path = f"{slug}/campaign.yaml"
    db.save_campaign_file(campaign_rel_path, yaml_content)
    db.save_campaign_file(f"{slug}/scripts/00_cold_outreach.yaml",
                          _yaml.dump(cold_script, default_flow_style=False, sort_keys=False, allow_unicode=True))
    db.save_campaign_file(f"{slug}/scripts/phone_00_cold_call.yaml",
                          _yaml.dump(phone_script, default_flow_style=False, sort_keys=False, allow_unicode=True))

    # Also write to the local cache so the campaign is immediately discoverable
    cache_dir = CAMPAIGNS_DIR / slug
    cache_dir.mkdir(parents=True, exist_ok=True)
    (cache_dir / "campaign.yaml").write_text(yaml_content)
    (cache_dir / "scripts").mkdir(exist_ok=True)
    (cache_dir / "scripts" / "00_cold_outreach.yaml").write_text(
        _yaml.dump(cold_script, default_flow_style=False, sort_keys=False, allow_unicode=True))
    (cache_dir / "scripts" / "phone_00_cold_call.yaml").write_text(
        _yaml.dump(phone_script, default_flow_style=False, sort_keys=False, allow_unicode=True))

    # Register the campaign in the campaigns table
    db.upsert_campaign(slug, str(cache_dir))

    # Invalidate cache so get_campaigns() picks up the new campaign
    _invalidate_campaign_cache()

    # Audit
    db.audit(
        current_user(request),
        "campaign.create",
        "campaign",
        slug,
        {"name": name, "product": product, "sources": prospect_sources, "channels": channels},
    )

    return _back("/admin/campaigns", msg=f"Campaign '{name}' created. Add leads via CSV import or sync from the detail page.")


@app.get("/admin/campaigns/{campaign_slug}", response_class=HTMLResponse)
async def admin_campaign_detail(request: Request, campaign_slug: str):
    """Edit a single campaign's plugin associations and view its YAML."""
    campaigns = get_campaigns()
    campaign = None
    for c in campaigns:
        if c.db_name == campaign_slug:
            campaign = c
            break

    if not campaign:
        return templates.TemplateResponse(request, "error.html", {
            "active": "admin",
            "message": f"Campaign '{campaign_slug}' not found.",
        }, status_code=404)

    yaml_path = campaign.config_dir / "campaign.yaml"
    yaml_content = yaml_path.read_text() if yaml_path.exists() else ""

    db = get_db()
    campaign_id = db.get_campaign_id(campaign.db_name)
    owner_role_id = next(r["id"] for r in db.list_roles() if r["name"] == access.OWNER_ROLE)
    return templates.TemplateResponse(request, "admin_campaign_detail.html", {
        "active": "admin",
        "campaign": campaign,
        "yaml": yaml_content,
        "plugins": _get_registry_plugins(),
        "networks": list(payments.NETWORKS),
        "unlocked": db.list_lead_packages(campaign_id) if campaign_id else [],
        "members": db.campaign_members(campaign.db_name),
        "owners": db.campaign_owners(campaign.db_name),
        "owner_candidates": [u for u in db.list_users() if u["is_active"] and owner_role_id in u["role_ids"]],
        "team": [u for u in db.list_users() if u["is_active"]],
        "roles": [r for r in db.list_roles() if r["name"] not in access.PROTECTED_ROLES],
        "paused": lead_packages.paused_refs(campaign),
        "default_rules": verify.DEFAULT_RULES.merged((campaign.lead_packages or {}).get("guarantee_rules")),
        "ratings": claims.ratings(db),
        "package_ref": lead_packages.package_ref,
    })


def _members_back(campaign_slug: str, *, msg: str = "", error: str = "") -> RedirectResponse:
    key, text = ("members_msg", msg) if msg else ("members_error", error)
    return RedirectResponse(url=f"/admin/campaigns/{quote(campaign_slug)}?{key}={quote(text)}", status_code=303)


@app.post("/admin/campaigns/{campaign_slug}/members")
async def admin_campaign_member_add(request: Request, campaign_slug: str, member: str = Form(...)):
    """Add a person or a role to a campaign; once it has members, only they (and Owners) see it."""
    if campaign_slug not in {c.db_name for c in get_campaigns()}:
        raise HTTPException(status_code=404, detail="Campaign not found")
    kind, _, raw_id = member.partition(":")
    if kind not in ("user", "role") or not raw_id.isdigit():
        return _members_back(campaign_slug, error="Pick a person or a role.")
    try:
        added = get_db().add_campaign_member(campaign_slug, current_user(request), **{f"{kind}_id": int(raw_id)})
    except AccessError as e:
        return _members_back(campaign_slug, error=str(e))
    return _members_back(campaign_slug, msg="Added. Only members (and Owners) see this campaign now."
                         if added else "They're already on this campaign.")


@app.post("/admin/campaigns/{campaign_slug}/owners")
async def admin_campaign_owner_add(request: Request, campaign_slug: str, user_id: int = Form(...)):
    """Super Admins: assign an Owner to a campaign; once it has any, other Owners can't see it."""
    if campaign_slug not in {c.db_name for c in get_campaigns()}:
        raise HTTPException(status_code=404, detail="Campaign not found")
    try:
        added = get_db().add_campaign_owner(campaign_slug, user_id, current_user(request))
    except AccessError as e:
        return _members_back(campaign_slug, error=str(e))
    return _members_back(campaign_slug, msg="Assigned. Other Owners no longer see this campaign."
                         if added else "They're already assigned to this campaign.")


@app.post("/admin/campaigns/{campaign_slug}/owners/{owner_id}/delete")
async def admin_campaign_owner_remove(request: Request, campaign_slug: str, owner_id: int):
    db = get_db()
    try:
        removed = db.remove_campaign_owner(campaign_slug, owner_id, current_user(request))
    except AccessError as e:
        return _members_back(campaign_slug, error=str(e))
    if not removed:
        return _members_back(campaign_slug, error="That Owner was not found.")
    return _members_back(campaign_slug, msg="Removed." if db.campaign_owners(campaign_slug) else
                         "Removed. With no Owners assigned, every Owner sees this campaign again.")


@app.post("/admin/campaigns/{campaign_slug}/members/{member_id}/delete")
async def admin_campaign_member_remove(request: Request, campaign_slug: str, member_id: int):
    db = get_db()
    if not db.remove_campaign_member(campaign_slug, member_id, current_user(request)):
        return _members_back(campaign_slug, error="That member was not found.")
    left = db.campaign_members(campaign_slug)
    return _members_back(campaign_slug, msg="Removed." if left else
                         "Removed. With no members left, everyone whose role allows it sees this campaign again.")


@app.post("/admin/campaigns/{campaign_slug}")
async def admin_campaign_update(
    request: Request,
    campaign_slug: str,
    prospect_sources: list[str] = Form(default=[]),
    product: str = Form(default=""),
    channels: list[str] = Form(default=[]),
    enrichers: list[str] = Form(default=[]),
    scheduler: str = Form(default=""),
    sender_name: str = Form(default=""),
    sender_email: str = Form(default=""),
    stale_threshold_days: str = Form(default="14"),
    lp_present: str = Form(default=""),
    lp_enabled: str = Form(default=""),
    lp_network: str = Form(default="base-sepolia"),
    lp_max_unlock_usd: str = Form(default="0"),
    lp_max_royalty_usd: str = Form(default="0"),
    lp_monthly_budget_usd: str = Form(default="0"),
    lp_rule_fail_at: str = Form(default=""),
    lp_rule_wrong_number_reports: str = Form(default=""),
    lp_rule_no_answer_attempts: str = Form(default=""),
    lp_rule_window_days: str = Form(default=""),
    lp_rule_claim_days: str = Form(default=""),
    lp_rule_unworked_at_close: str = Form(default=""),
    lp_listed: list[str] = Form(default=[]),
    lp_active: list[str] = Form(default=[]),
):
    """Update a campaign's plugin associations by rewriting campaign.yaml."""
    if not _same_origin(request):
        return _back("/admin/campaigns", error="Cross-site request blocked.")

    campaigns = get_campaigns()
    campaign = None
    for c in campaigns:
        if c.db_name == campaign_slug:
            campaign = c
            break

    if not campaign:
        return _back("/admin/campaigns", error=f"Campaign '{campaign_slug}' not found.")

    # Load existing YAML, update the plugin fields
    import yaml as _yaml
    yaml_path = campaign.config_dir / "campaign.yaml"
    raw = _yaml.safe_load(yaml_path.read_text())

    # Update plugin associations
    raw["prospect_sources"] = prospect_sources if prospect_sources else raw.get("prospect_sources", [])
    if product:
        raw["product"] = product
    new_channels = channels if channels else raw.get("channels", [])
    problem = access.channel_change_problem(current_user(request), raw.get("channels", []), new_channels)
    if problem:
        return _back("/admin/campaigns", error=problem)
    raw["channels"] = new_channels
    raw["enrichers"] = enrichers if enrichers else raw.get("enrichers", [])
    if scheduler:
        raw["scheduler"] = scheduler
    elif "scheduler" in raw:
        del raw["scheduler"]  # remove if empty
    if sender_name:
        raw["sender_name"] = sender_name
    if sender_email:
        raw["sender_email"] = sender_email
    if stale_threshold_days:
        try:
            raw["stale_threshold_days"] = int(stale_threshold_days)
        except ValueError:
            pass

    # Lead packages: only when the section was on the form, so older forms
    # (and other tools posting here) never turn spending on or off by accident.
    if lp_present:
        def dollars(value: str) -> float:
            try:
                return round(max(0.0, float(value)), 2)
            except ValueError:
                return 0.0
        block = dict(raw.get("lead_packages") or {})
        block.update({
            "enabled": bool(lp_enabled),
            "network": lp_network if lp_network in payments.NETWORKS else "base-sepolia",
            "max_unlock_usd": dollars(lp_max_unlock_usd),
            "max_royalty_per_contact_usd": dollars(lp_max_royalty_usd),
            "monthly_budget_usd": dollars(lp_monthly_budget_usd),
        })
        # Unlocked packages left unticked are paused: their leads aren't contacted.
        # Only packages this form listed change, so one unlocked meanwhile stays active.
        campaign_id = get_db().get_campaign_id(campaign.db_name)
        unlocked = get_db().list_lead_packages(campaign_id) if campaign_id else []
        listed = set(lp_listed)
        paused = []
        for ref in (lead_packages.package_ref(p["provider"], p["package_id"]) for p in unlocked):
            if (ref in listed and ref not in lp_active) or (ref not in listed and ref in lead_packages.paused_refs(campaign)):
                paused.append(ref)
        block.pop("paused_packages", None)
        if paused:
            block["paused_packages"] = paused
        # Default guarantee rules for this campaign's future unlocks (range-checked;
        # weights stay as set in campaign.yaml).
        entered = {"fail_at": lp_rule_fail_at, "wrong_number_reports": lp_rule_wrong_number_reports,
                   "no_answer_attempts": lp_rule_no_answer_attempts, "window_days": lp_rule_window_days,
                   "claim_days": lp_rule_claim_days, "unworked_at_close": lp_rule_unworked_at_close}
        current = dict(block.get("guarantee_rules") or {})
        checked = verify.DEFAULT_RULES.merged({**current, **{k: v for k, v in entered.items() if v.strip()}})
        block["guarantee_rules"] = {**{k: v for k, v in checked.to_dict().items() if k != "weights"},
                                    **({"weights": current["weights"]} if current.get("weights") else {})}
        raw["lead_packages"] = block
        get_db().audit(current_user(request), "campaign.lead_packages", "campaign", campaign.db_name, block)

    # Write back
    save_campaign_file(yaml_path, _yaml.dump(raw, default_flow_style=False, sort_keys=False))

    return _back("/admin/campaigns", msg=f"Updated campaign '{campaign.name}'.")


# ── CSV Lead Import ─────────────────────────────────────────────────

# Maps CSV column headers (case-insensitive) to Prospect model fields.
# Multiple header variants are accepted for each field.
_CSV_FIELD_MAP = {
    "name": ["name", "organization", "organization name", "org", "company"],
    "ein": ["ein", "tax id", "tax_id"],
    "ntee_code": ["ntee", "ntee_code", "ntee code", "category"],
    "website_url": ["website", "website_url", "url", "domain"],
    "address": ["address", "street", "address1", "address_1", "street_address"],
    "city": ["city"],
    "state": ["state", "st"],
    "zip": ["zip", "zip_code", "zipcode", "postal", "postal_code"],
    "county": ["county"],
    "focus_area": ["focus_area", "focus area", "focus", "category"],
    "annual_revenue": ["annual_revenue", "annual revenue", "revenue", "income"],
    "voter_engagement": ["voter_engagement", "voter engagement"],
    "source": ["source", "lead_source", "lead source"],
    "source_url": ["source_url", "source url", "referral", "ref"],
    # Outreach-level contact info (goes on the outreach row, not prospect)
    "contact_name": ["contact_name", "contact", "contact name", "first name", "firstname"],
    "contact_email": ["contact_email", "email", "contact email", "e-mail"],
    "contact_phone": ["contact_phone", "phone", "contact phone", "telephone", "tel"],
    "contact_title": ["contact_title", "contact title", "title", "position"],
}


def _parse_csv_headers(headers: list[str]) -> dict[str, int]:
    """Map CSV column headers to field names. Returns {field: col_index}."""
    mapping = {}
    normalized = [h.strip().lower() for h in headers]
    for field, variants in _CSV_FIELD_MAP.items():
        for i, col in enumerate(normalized):
            if col in variants and field not in mapping:
                mapping[field] = i
    return mapping


@app.post("/admin/campaigns/{campaign_slug}/import-csv")
async def import_csv_leads(
    request: Request,
    campaign_slug: str,
    file: UploadFile = File(...),
):
    """Upload a CSV file of leads and add them to a campaign.

    Accepts any CSV with a header row. Recognized columns (case-insensitive):
    name, organization, ein, ntee, website, address, city, state, zip,
    county, focus_area, annual_revenue, voter_engagement, source,
    contact_name, contact_email, contact_phone, contact_title

    Minimum required: name (and state recommended for dedup).

    Prospects are upserted (matched by EIN or name+state). If the prospect
    is new to this campaign, an outreach row is created at 'cold' stage.
    Contact info from the CSV is written to the outreach row.
    """
    if not _same_origin(request):
        return _back("/admin/campaigns", error="Cross-site request blocked.")

    # Find campaign
    campaigns = get_campaigns()
    campaign = next((c for c in campaigns if c.db_name == campaign_slug), None)
    if not campaign:
        return _back("/admin/campaigns", error=f"Campaign '{campaign_slug}' not found.")

    # Read and parse the CSV
    import csv as _csv
    import io

    raw = await file.read()
    if not raw:
        return _back(f"/admin/campaigns/{campaign_slug}", error="Empty file.")

    try:
        text = raw.decode("utf-8-sig")  # handles BOM
    except UnicodeDecodeError:
        try:
            text = raw.decode("latin-1")
        except Exception:
            return _back(f"/admin/campaigns/{campaign_slug}", error="Could not decode file. Use UTF-8.")

    reader = _csv.reader(io.StringIO(text))
    rows = list(reader)
    if len(rows) < 2:
        return _back(f"/admin/campaigns/{campaign_slug}", error="CSV needs a header row and at least one data row.")

    field_map = _parse_csv_headers(rows[0])
    if "name" not in field_map:
        return _back(
            f"/admin/campaigns/{campaign_slug}",
            error="CSV must have a 'name' or 'organization' column.",
        )

    db = get_db()
    from core.models import Prospect
    import json as _json

    campaign_id = db.get_campaign_id(campaign.db_name) or db.upsert_campaign(campaign.db_name, str(campaign.config_dir))

    imported = 0
    updated = 0
    skipped = 0
    errors = []

    for row_idx, row in enumerate(rows[1:], start=2):
        if not row or all(not c.strip() for c in row):
            skipped += 1
            continue

        def get(field):
            idx = field_map.get(field)
            if idx is None or idx >= len(row):
                return ""
            return row[idx].strip()

        org_name = get("name")
        if not org_name:
            skipped += 1
            continue

        # Parse annual_revenue as int
        rev_str = get("annual_revenue").replace(",", "").replace("$", "")
        try:
            annual_revenue = int(rev_str) if rev_str else None
        except ValueError:
            annual_revenue = None

        # Parse voter_engagement as bool
        ve_str = get("voter_engagement").lower()
        voter_engagement = ve_str in ("1", "true", "yes", "y")

        prospect = Prospect(
            name=org_name,
            ein=get("ein") or None,
            ntee_code=get("ntee_code") or None,
            website_url=get("website_url") or None,
            address=get("address") or None,
            city=get("city") or None,
            state=get("state") or None,
            zip=get("zip") or None,
            county=get("county") or None,
            focus_area=get("focus_area") or None,
            annual_revenue=annual_revenue,
            voter_engagement=voter_engagement,
            source=get("source") or "csv_upload",
            source_url=get("source_url") or None,
            metadata={},
        )

        try:
            prospect_id = db.upsert_prospect(prospect)
            # Check if this was an insert or update
            existing = db.conn.execute(
                "SELECT created_at, updated_at FROM prospects WHERE id = ?",
                (prospect_id,),
            ).fetchone()
            is_new = existing and existing["created_at"] == existing["updated_at"]

            # Create outreach row for this campaign
            outreach_id = db.upsert_outreach(prospect_id, campaign_id)

            # If CSV has contact info, update the outreach row
            contact_name = get("contact_name")
            contact_email = get("contact_email")
            contact_phone = get("contact_phone")
            contact_title = get("contact_title")
            if contact_name or contact_email or contact_phone:
                db.update_outreach(outreach_id, {
                    "contact_name": contact_name,
                    "contact_email": contact_email,
                    "contact_phone": contact_phone,
                    "contact_title": contact_title,
                })

            if is_new:
                imported += 1
            else:
                updated += 1
        except Exception as exc:
            errors.append(f"Row {row_idx}: {exc}")
            skipped += 1

    db.audit(
        current_user(request),
        "csv_import",
        "campaign",
        campaign.db_name,
        {"file": file.filename, "imported": imported, "updated": updated, "skipped": skipped, "errors": errors[:10]},
    )

    msg = f"Imported {imported} new, updated {updated} existing, skipped {skipped}."
    if errors:
        msg += f" {len(errors)} error(s): {errors[0]}"
    return _back(f"/admin/campaigns/{campaign_slug}", msg=msg)


@app.get("/admin/campaigns/{campaign_slug}/import-template")
async def download_csv_template():
    """Download a blank CSV template with the recognized column headers."""
    from fastapi.responses import PlainTextResponse
    headers = [
        "name", "ein", "ntee_code", "website_url", "address",
        "city", "state", "zip", "county", "focus_area",
        "annual_revenue", "voter_engagement", "source",
        "contact_name", "contact_email", "contact_phone", "contact_title",
    ]
    return PlainTextResponse(
        ",".join(headers) + "\n",
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=lead_import_template.csv"},
    )


@app.get("/healthz")
async def healthz():
    return {"ok": True}


@app.get("/api/stats")
async def api_stats(request: Request):
    """JSON API for pipeline stats — useful for external dashboards."""
    db = get_db()
    campaigns = visible_campaigns(request)
    results = []
    for c in campaigns:
        results.append(db.get_pipeline_stats(c.db_name))
    return JSONResponse(results)


# Last, so every route above wins: /mcp, the OAuth endpoints (/authorize,
# /token, /register, /revoke) and their /.well-known metadata (web/mcp_server.py).
app.mount("/", mcp_mount)


def free_port(start: int = 8000, tries: int = 20, host: str = "0.0.0.0") -> int:
    """The first port from `start` that nothing is listening on (for local runs)."""
    import socket

    for port in range(start, start + tries):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)  # as uvicorn does
            try:
                sock.bind((host, port))
            except OSError:
                continue
            return port
    raise SystemExit(f"agency-os: ports {start}-{start + tries - 1} are all in use; set PORT to pick one")


if __name__ == "__main__":
    import uvicorn

    # An explicit PORT (Railway, Docker) is used exactly; locally, step past ports already in use.
    if not os.environ.get("PORT"):
        os.environ["PORT"] = str(free_port())  # site_url() builds local links from it
        if os.environ["PORT"] != "8000":
            print(f"agency-os: port 8000 is in use; using {os.environ['PORT']}", flush=True)
    print(f"agency-os: http://127.0.0.1:{os.environ['PORT']}", flush=True)
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ["PORT"]))