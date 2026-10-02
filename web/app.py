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
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from datetime import datetime

# Ensure project root is on path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from fastapi import FastAPI, Request, Query, HTTPException, Form
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from core.db import Database
from core.campaign import discover_campaigns
from core.registry import PluginRegistry
from core.pipeline import Pipeline

# ── Init ────────────────────────────────────────────────────────────

app = FastAPI(title="agency-os", docs_url=None, redoc_url=None)

templates = Jinja2Templates(directory=str(PROJECT_ROOT / "web" / "templates"))
app.mount("/static", StaticFiles(directory=str(PROJECT_ROOT / "web" / "static")), name="static")

DB_PATH = os.environ.get("AGENCY_OS_DB", str(PROJECT_ROOT / "db.sqlite"))


def get_db() -> Database:
    db = Database(DB_PATH)
    _ = db.conn  # ensure schema is created
    return db


def get_campaigns():
    return discover_campaigns(str(PROJECT_ROOT / "campaigns"))


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


templates.env.filters["currency"] = fmt_currency
templates.env.filters["fmt_date"] = fmt_date


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

    return templates.TemplateResponse("dashboard.html", {
        "request": request,
        "campaigns": all_stats,
        "total_prospects": total_prospects,
        "total_emails": total_emails,
    })


@app.get("/prospects", response_class=HTMLResponse)
async def prospect_list(
    request: Request,
    q: str = Query(default="", description="Search name, city, EIN, zip, focus area, website"),
    source: str = Query(default="", description="Filter by source"),
    stage: str = Query(default="", description="Filter by stage"),
    cities: str = Query(default="", description="Comma-separated city filter"),
    sort: str = Query(default="name", description="Sort column"),
    dir: str = Query(default="asc", description="Sort direction: asc or desc"),
    page: int = Query(default=1, ge=1),
    per_page: int = Query(default=50, ge=10, le=500),
    print: int = Query(default=0, description="Print view: 1 = all rows, no pagination"),
):
    """Prospect list with search, filters, sortable columns, pagination, print view."""
    db = get_db()
    campaigns = get_campaigns()
    campaign_map = {c.db_name: c for c in campaigns}

    # Build query — search across multiple fields
    where_parts = []
    params = []

    if q:
        where_parts.append(
            "(p.name LIKE ? OR p.city LIKE ? OR p.ein LIKE ? "
            "OR p.zip LIKE ? OR p.focus_area LIKE ? OR p.website_url LIKE ? "
            "OR p.county LIKE ? OR p.ntee_code LIKE ?)"
        )
        params.extend([f"%{q}%"] * 8)

    if source:
        where_parts.append("p.source = ?")
        params.append(source)

    if stage:
        where_parts.append("o.stage = ?")
        params.append(stage)

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

        return templates.TemplateResponse("prospects_print.html", {
            "request": request,
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

    return templates.TemplateResponse("prospects.html", {
        "request": request,
        "prospects": rows,
        "sources": sources,
        "cities_list": cities_list,
        "campaigns": campaign_map,
        "q": q,
        "source_filter": source,
        "stage_filter": stage,
        "cities_filter": cities,
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

    return templates.TemplateResponse(template_name, {
        "request": request,
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

    return templates.TemplateResponse("prospect_detail.html", {
        "request": request,
        "prospect": prospect,
        "outreach_rows": outreach_rows,
        "email_logs": email_logs,
        "activity_logs": {r["id"]: json.loads(r["activity_log"] or "[]") for r in outreach_rows},
    })


@app.post("/prospects/{prospect_id}/stage")
async def update_stage(
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
    return RedirectResponse(url=f"/prospects/{prospect_id}", status_code=303)


@app.post("/prospects/{prospect_id}/contact")
async def update_contact(
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

    return templates.TemplateResponse("campaigns.html", {
        "request": request,
        "campaigns": campaign_data,
    })


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

    return templates.TemplateResponse("emails.html", {
        "request": request,
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
    uvicorn.run(app, host="0.0.0.0", port=8000)