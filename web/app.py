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
from pathlib import Path
from datetime import datetime
from urllib.parse import quote, urlsplit, urlencode

# Ensure project root is on path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from fastapi import FastAPI, Request, Query, HTTPException, Form, Depends
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from core import access
from core.access import AccessError, CurrentUser
from core.db import Database
from core.campaign import discover_campaigns, sync_campaign_files
from core.registry import PluginRegistry
from core.pipeline import Pipeline
from plugins.channels.lob_direct_mail import TEMPLATE_ID_RE, lob_template_url
from core.jobs import JobRunner, configured_jobs, jobs_enabled

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


def save_campaign_file(path: Path, content: str) -> None:
    """Write a campaign file to the local cache and to the database."""
    path.write_text(content)
    get_db().save_campaign_file(path.resolve().relative_to(CAMPAIGNS_DIR.resolve()).as_posix(), content)


def get_job_runner() -> JobRunner:
    return JobRunner(get_db().url, str(PROJECT_ROOT / "plugins"), get_campaigns)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    bootstrap_access()
    # Background jobs (core/jobs.py): on for the deployed service only.
    task = asyncio.create_task(get_job_runner().loop()) if jobs_enabled() else None
    yield
    if task:
        task.cancel()


app = FastAPI(title="agency-os", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)

templates = Jinja2Templates(directory=str(PROJECT_ROOT / "web" / "templates"))
app.mount("/static", StaticFiles(directory=str(PROJECT_ROOT / "web" / "static")), name="static")

# ── Auth ────────────────────────────────────────────────────────────
# Multi-user login with role-based permissions (policy in core/access.py).
# Every route is checked against access.ROUTE_RULES before its handler
# runs; a route missing from that map is denied for everyone.

SESSION_COOKIE = "aos_session"
SESSION_TTL_DAYS = 14
LOGIN_WINDOW_SECONDS = 15 * 60
LOGIN_MAX_FAILURES = 5
_login_failures: dict[str, list[float]] = defaultdict(list)
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
    token = request.cookies.get(SESSION_COOKIE)
    user_id = db.session_user_id(access.hash_token(token)) if token else None
    user = db.load_current_user(user_id) if user_id else None
    if user is None:
        raise LoginRequired()
    request.state.user = user
    if rule is None or not user.allows(rule):
        raise Forbidden()
    if request.method not in ("GET", "HEAD", "OPTIONS") and not _same_origin(request):
        raise Forbidden()


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


def fmt_date(val) -> str:
    if not val:
        return "—"
    if isinstance(val, str):
        try:
            val = datetime.fromisoformat(val)
        except ValueError:
            return val
    return val.strftime("%b %d, %Y")


def nav_active(path: str) -> str:
    """The nav item to highlight for a URL path, e.g. "/prospects/12" -> "prospects"."""
    if path == "/":
        return "dashboard"
    parts = path.strip("/").split("/")
    if parts[0] == "admin":
        return "admin-campaigns" if parts[1:2] == ["campaigns"] else "admin"
    return parts[0]


templates.env.filters["currency"] = fmt_currency
templates.env.filters["fmt_date"] = fmt_date
templates.env.globals["nav_active"] = nav_active


# ── Routes ──────────────────────────────────────────────────────────


@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    """Main dashboard — pipeline overview across all campaigns."""
    db = get_db()
    campaigns = get_campaigns()

    all_stats = []
    for c in campaigns:
        stats = db.get_pipeline_stats(c.db_name)
        stats["config"] = c
        all_stats.append(stats)

    # Aggregate totals
    total_prospects = sum(s["total_prospects"] for s in all_stats)
    total_emails = sum(s["total_emails_sent"] for s in all_stats)

    return templates.TemplateResponse(request, "dashboard.html", {
        "campaigns": all_stats,
        "total_prospects": total_prospects,
        "total_emails": total_emails,
    })


SAVED_LIST_FIELDS = ("q", "source", "stage", "cities", "campaign", "sort", "dir", "per_page")


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


@app.get("/prospects", response_class=HTMLResponse)
async def prospect_list(
    request: Request,
    q: str = Query(default="", description="Search name, city, EIN, zip, focus area, website"),
    source: str = Query(default="", description="Filter by source"),
    stage: str = Query(default="", description="Filter by stage"),
    cities: str = Query(default="", description="Comma-separated city filter"),
    campaign: str = Query(default="", description="Filter by campaign slug"),
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
    campaigns = get_campaigns()
    campaign_map = {c.db_name: c for c in campaigns}

    # Build query — search across multiple fields
    where_parts = []
    params = []

    if q:
        where_parts.append(
            "(p.name ILIKE ? OR p.city ILIKE ? OR p.ein ILIKE ? "
            "OR p.zip ILIKE ? OR p.focus_area ILIKE ? OR p.website_url ILIKE ? "
            "OR p.county ILIKE ? OR p.ntee_code ILIKE ?)"
        )
        params.extend([f"%{q}%"] * 8)

    if source:
        where_parts.append("p.source = ?")
        params.append(source)

    if stage:
        where_parts.append("o.stage = ?")
        params.append(stage)

    # Campaign filter — only show prospects in the selected campaign
    if campaign:
        where_parts.append("o.campaign_id = (SELECT id FROM campaigns WHERE name = ?)")
        params.append(campaign)

    # City filter — supports multiple cities (comma-separated)
    if cities:
        city_list = [c.strip() for c in cities.split(",") if c.strip()]
        if city_list:
            placeholders = ",".join("?" * len(city_list))
            where_parts.append(f"p.city IN ({placeholders})")
            params.extend(city_list)

    where_clause = " AND ".join(where_parts) if where_parts else "1=1"

    # Count total
    count_sql = f"""
        SELECT COUNT(*) FROM prospects p
        LEFT JOIN outreach o ON p.id = o.prospect_id
        WHERE {where_clause}
    """
    total = db.conn.execute(count_sql, params).fetchone()[0]

    # Sort — every column is sortable, with direction toggle
    sort_map = {
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
    sort_col = sort_map.get(sort, "p.name")
    sort_dir = "DESC" if dir.lower() == "desc" else "ASC"
    if sort == "revenue" and dir == "asc" and sort not in request.query_params:
        sort_dir = "DESC"
    order = f"{sort_col} {sort_dir}"

    # Nulls last for DESC, nulls first for ASC
    if sort_dir == "DESC":
        order = f"CASE WHEN {sort_col} IS NULL THEN 1 ELSE 0 END, {sort_col} DESC"
    else:
        order = f"CASE WHEN {sort_col} IS NULL THEN 1 ELSE 0 END, {sort_col} ASC"

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
            "SELECT DISTINCT city FROM prospects WHERE city IS NOT NULL AND city != '' ORDER BY city"
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
        "SELECT DISTINCT source FROM prospects WHERE source IS NOT NULL ORDER BY source"
    ).fetchall()]

    # Get distinct cities for filter dropdown (top 30 by prospect count)
    city_rows = db.conn.execute("""
        SELECT city, COUNT(*) as cnt FROM prospects
        WHERE city IS NOT NULL AND city != ''
        GROUP BY city ORDER BY cnt DESC LIMIT 30
    """).fetchall()
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
        cities=cities, campaign=campaign, sort=sort, dir=sort_dir.lower(), per_page=per_page))
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

    events = db.get_upcoming_events(days=90)  # fetch wider range for month nav

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

    feed_url = f"http://localhost:8000/calendar.ics?days={days}"

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
async def calendar_ics(days: int = Query(default=90, ge=1, le=365)):
    """ICS calendar feed — subscribe in any calendar app."""
    from fastapi.responses import PlainTextResponse
    from core.ics import generate_ics

    db = get_db()
    events = db.get_upcoming_events(days=days)
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
    prospect_options = db.conn.execute(
        """SELECT p.id, p.name, p.city FROM prospects p
           ORDER BY p.name LIMIT 50"""
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
        preview_p = db.get_prospect(preview_prospect)

    # Get prospects for preview dropdown
    prospect_options = db.conn.execute(
        """SELECT p.id, p.name, p.city FROM prospects p
           WHERE p.address IS NOT NULL AND p.address != ''
           ORDER BY p.name LIMIT 50"""
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
    calls = db.get_all_calls(limit=per_page, offset=offset, outcome=outcome, interest=interest)

    # Count total
    where_parts = []
    params = []
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

    stats = db.get_call_stats()
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
):
    """Record a completed phone call."""
    from core.models import CallLog
    db = get_db()

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
    )
    db.log_call(call)
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
        prospect_options = db.conn.execute(
            """SELECT p.id, p.name, p.city FROM prospects p
               JOIN outreach o ON p.id = o.prospect_id
               WHERE o.contact_phone IS NOT NULL OR o.contact_email IS NOT NULL
               ORDER BY p.name LIMIT 100"""
        ).fetchall()

    template_name = "call_scripts_print.html" if print else "call_scripts.html"

    return templates.TemplateResponse(request, template_name, {
        "scripts": all_scripts,
        "prospect": prospect,
        "prospect_options": prospect_options,
        "stage_filter": stage,
        "now": _dt.now().strftime("%B %d, %Y at %I:%M %p"),
    })


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

    # u9itus demo page (A6): status kept on the prospect by provisioning and
    # pull-events, plus the portal events logged on the outreach rows.
    activity_logs = {r["id"]: json.loads(r["activity_log"] or "[]") for r in outreach_rows}
    portal_events = sorted(
        (e for log in activity_logs.values() for e in log if str(e.get("ref", "")).startswith("u9itus:")),
        key=lambda e: e.get("timestamp", ""), reverse=True,
    )

    return templates.TemplateResponse(request, "prospect_detail.html", {
        "portal": (prospect.metadata or {}).get("u9itus") or {},
        "portal_link": next((r["demo_link"] for r in outreach_rows if r["demo_link"]), None),
        "portal_events": portal_events,
        "ready_to_close": any(e.get("flag") == "ready_to_close" for e in portal_events),
        "portal_available": _portal_campaign(outreach_rows) is not None,
        "msg": request.query_params.get("msg", ""),
        "error": request.query_params.get("error", ""),
        "prospect": prospect,
        "outreach_rows": outreach_rows,
        "email_logs": email_logs,
        "call_history": call_history,
        "activity_logs": activity_logs,
    })


def _plugin_registry() -> PluginRegistry:
    registry = PluginRegistry()
    registry.discover(str(PROJECT_ROOT / "plugins"))
    return registry


def _portal_campaign(outreach_rows):
    """The first of the prospect's campaigns whose product makes u9itus demo pages."""
    names = {r["campaign_name"] for r in outreach_rows}
    registry = _plugin_registry()
    for campaign in get_campaigns():
        if campaign.db_name in names and hasattr(registry.get_product(campaign.product), "provision_demo"):
            return campaign
    return None


@app.post("/prospects/{prospect_id}/portal")
async def prospect_portal(request: Request, prospect_id: int, action: str = Form(...)):
    """Create, renew, or check a prospect's u9itus demo page (A6)."""
    db = get_db()
    rows = db.conn.execute(
        """SELECT o.*, c.name as campaign_name FROM outreach o
           JOIN campaigns c ON o.campaign_id = c.id WHERE o.prospect_id = ?""",
        (prospect_id,),
    ).fetchall()
    campaign = _portal_campaign(rows)
    back = f"/prospects/{prospect_id}"
    if campaign is None:
        return _back(back, error="None of this prospect's campaigns make u9itus demo pages.")

    if action not in ("provision", "renew", "status"):
        return _back(back, error="Unknown action.")

    def call_u9itus() -> dict:
        # Runs in a worker thread (the API call can take seconds); database
        # connections are per thread, so open one here.
        pipeline = Pipeline(get_db(), _plugin_registry())
        if action == "status":
            return pipeline.refresh_portal_status(campaign, prospect_id)
        return pipeline.provision_prospect(campaign, prospect_id, refresh=action == "renew")

    result = await asyncio.to_thread(call_u9itus)
    done = {"provision": "Demo page ready.", "renew": "Demo page renewed with a new link.",
            "status": "Status updated."}[action]

    if result.get("error"):
        return _back(back, error=f"u9itus: {result.get('detail') or 'request failed'}")
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
    campaigns = get_campaigns()

    campaign_data = []
    for c in campaigns:
        stats = db.get_pipeline_stats(c.db_name)
        campaign_data.append({
            "config": c,
            "stats": stats,
        })

    return templates.TemplateResponse(request, "campaigns.html", {
        "campaigns": campaign_data,
    })


@app.get("/plugins", response_class=HTMLResponse)
async def plugins_page(request: Request):
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
    })


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

    where_parts = []
    params = []
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
    ).fetchall()]

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
    now = time.monotonic()
    recent = [t for t in _login_failures[key] if now - t < LOGIN_WINDOW_SECONDS]
    _login_failures[key] = recent

    def fail(message: str):
        return RedirectResponse(
            url=f"/login?error={quote(message)}&next={quote(next)}", status_code=303
        )

    if len(recent) >= LOGIN_MAX_FAILURES:
        return fail("Too many failed attempts. Try again in 15 minutes.")

    db = get_db()
    row = db.get_user_by_email(email)
    # Always run a hash check so response time doesn't reveal which emails exist
    ok = access.verify_password(password, row["password_hash"] if row else _DUMMY_HASH)
    if not (row and ok and row["is_active"]):
        _login_failures[key].append(now)
        return fail("Incorrect email or password.")

    _login_failures.pop(key, None)
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


@app.get("/welcome/{token}", response_class=HTMLResponse)
async def welcome_page(request: Request, token: str, error: str = Query(default="")):
    """Landing page for the one-time link in a welcome email (core/welcome.py)."""
    db = get_db()
    user_id = db.invite_user_id(access.hash_token(token))
    invitee = db.load_current_user(user_id) if user_id else None
    return templates.TemplateResponse(request, "welcome.html", {
        "invitee": invitee,
        "error": error,
    }, status_code=200 if invitee else 410)


@app.post("/welcome/{token}")
async def accept_welcome(
    request: Request,
    token: str,
    new_password: str = Form(...),
    confirm_password: str = Form(...),
):
    if new_password != confirm_password:
        return RedirectResponse(
            url=f"/welcome/{quote(token)}?error=Passwords+don%27t+match.", status_code=303
        )
    db = get_db()
    try:
        user_id = db.accept_invite(access.hash_token(token), new_password)
    except AccessError as e:
        return RedirectResponse(url=f"/welcome/{quote(token)}?error={quote(str(e))}", status_code=303)
    user = db.load_current_user(user_id)
    db.audit(user, "auth.invite_accepted", "user", user.id)
    return _start_session(request, user, user.landing_page())


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
    user = current_user(request)
    return templates.TemplateResponse(request, "account.html", {
        "active": "account",
        "permissions": [(k, v) for k, v in access.CATALOG.items() if user.can(k)],
        "msg": msg,
        "error": error,
    })


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


def _back(path: str, *, msg: str = "", error: str = "") -> RedirectResponse:
    qs = f"?msg={quote(msg)}" if msg else f"?error={quote(error)}" if error else ""
    return RedirectResponse(url=f"{path}{qs}", status_code=303)


@app.get("/admin/users", response_class=HTMLResponse)
async def admin_users(request: Request, msg: str = Query(default=""), error: str = Query(default="")):
    db = get_db()
    return templates.TemplateResponse(request, "admin_users.html", {
        "active": "admin",
        "users": db.list_users(),
        "roles": db.list_roles(),
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
):
    try:
        get_db().create_user(email, name, password, role_ids, current_user(request))
    except AccessError as e:
        return _back("/admin/users", error=str(e))
    return _back("/admin/users", msg=f"Added {email.strip().lower()}.")


@app.post("/admin/users/{user_id}")
async def admin_update_user(
    request: Request,
    user_id: int,
    name: str = Form(...),
    is_active: str = Form(default=""),
    role_ids: list[int] = Form(default=[]),
    new_password: str = Form(default=""),
):
    actor = current_user(request)
    db = get_db()
    try:
        db.update_user(user_id, name=name, is_active=bool(is_active), role_ids=role_ids, actor=actor)
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
        "entries": get_db().list_audit(),
    })


@app.get("/admin/jobs", response_class=HTMLResponse)
async def admin_jobs(request: Request, msg: str = Query(default=""), error: str = Query(default="")):
    runner = get_job_runner()
    last = runner.last_runs()
    due = {job.key for job in runner.due_jobs()}
    return templates.TemplateResponse(request, "admin_jobs.html", {
        "active": "admin",
        "jobs": [{"job": job, "last": last.get(job.key), "due": job.key in due} for job in configured_jobs()],
        "runs": runner.recent_runs(),
        "enabled": jobs_enabled(),
        "api_configured": bool(os.environ.get("U9ITUS_BASE_URL") and os.environ.get("U9ITUS_AGENCY_TOKEN")),
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
    campaigns = get_campaigns()
    db = get_db()

    campaign_data = []
    for c in campaigns:
        stats = db.get_pipeline_stats(c.db_name)
        # Read the raw YAML for display
        yaml_path = c.config_dir / "campaign.yaml"
        yaml_content = yaml_path.read_text() if yaml_path.exists() else ""
        campaign_data.append({
            "config": c,
            "stats": stats,
            "yaml": yaml_content,
        })

    return templates.TemplateResponse(request, "admin_campaigns.html", {
        "active": "admin",
        "campaigns": campaign_data,
        "plugins": _get_registry_plugins(),
        "msg": msg,
        "error": error,
    })


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

    return templates.TemplateResponse(request, "admin_campaign_detail.html", {
        "active": "admin",
        "campaign": campaign,
        "yaml": yaml_content,
        "plugins": _get_registry_plugins(),
    })


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
    raw["channels"] = channels if channels else raw.get("channels", [])
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

    # Write back
    save_campaign_file(yaml_path, _yaml.dump(raw, default_flow_style=False, sort_keys=False))

    return _back("/admin/campaigns", msg=f"Updated campaign '{campaign.name}'.")


@app.get("/healthz")
async def healthz():
    return {"ok": True}


@app.get("/api/stats")
async def api_stats():
    """JSON API for pipeline stats — useful for external dashboards."""
    db = get_db()
    campaigns = get_campaigns()
    results = []
    for c in campaigns:
        results.append(db.get_pipeline_stats(c.db_name))
    return JSONResponse(results)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))