"""
agency-os CLI.

Usage:
    agency-os sync --campaign voter-guide-cbo
    agency-os sync --all
    agency-os enqueue --campaign voter-guide-cbo --limit 50
    agency-os enrich --campaign voter-guide-cbo
    agency-os stale --all
    agency-os bookings --campaign voter-guide-cbo
    agency-os digest --campaign voter-guide-cbo
    agency-os campaigns
    agency-os plugins
    agency-os new-plugin grant-finder --title "Grant finder"
    agency-os import-sqlite --from db.sqlite
    agency-os users list
    agency-os users create-owner --email you@example.com
    agency-os users grant-owner --email someone@example.com
    agency-os users grant-super-admin --email you@example.com
    agency-os users set-password --email someone@example.com
    agency-os users invite --email rep@example.com --name "Jane Rep" --role "Sales Rep"
    agency-os packages list
    agency-os packages unlock --campaign voter-guide-cbo --provider https://leads.example --package p1 --email you@example.com
    agency-os spend --campaign voter-guide-cbo
    agency-os spend allowance --email rep@example.com --usd 100
    agency-os packages verify --campaign voter-guide-cbo [--ai]
    agency-os packages claim --id 3 --email you@example.com
    agency-os spend pending
    agency-os accounts platform-key
    agency-os accounts create --ref org_42 --name "Acme Realty"
    agency-os searches run
    agency-os generator run [--id 4]
    agency-os spend resolve --id 12 --status settled --tx 0x...
    agency-os connect --url https://your-app.up.railway.app --key aos_cli_...
    agency-os remote users invite --email rep@example.com --role Caller
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

import click

from core import access
from core.access import AccessError, OWNER_ROLE, SUPER_ADMIN_ROLE
from core.campaign import discover_campaigns, sync_campaign_files
from core.db import Database, redact_url
from core.pipeline import Pipeline
from core.registry import PluginRegistry


def _setup(campaigns_dir: str = "campaigns", plugins_dir: str = "plugins", db_url: str = ""):
    """Initialize registry + DB, discover plugins + campaigns.

    Campaign files come from the database (seeded from campaigns_dir), so the
    CLI sees the same campaign config the dashboard edits.
    """
    registry = PluginRegistry()
    print("Discovering plugins...")
    registry.discover(plugins_dir)
    db = Database(db_url or None)
    print(f"Database: {redact_url(db.url)}")
    print("Discovering campaigns...")
    cache_dir = os.environ.get("AGENCY_OS_CAMPAIGNS_DIR",
                               str(Path(tempfile.gettempdir()) / "agency-os-campaigns"))
    campaigns = discover_campaigns(sync_campaign_files(db, campaigns_dir, cache_dir))
    for c in campaigns:
        print(f"  + {c.db_name}")
    return registry, db, campaigns


def _get_campaign(campaigns: list, name: str):
    """Find a campaign by its full name or slug, or by a short name such as
    "voter-guide-cbo" when its words match exactly one campaign's slug."""
    name_slug = name.lower().replace(" ", "-").replace("_", "-")
    for c in campaigns:
        if c.db_name == name_slug or c.name.lower() == name.lower():
            return c
    words = [w for w in name_slug.split("-") if w]
    matches = [c for c in campaigns if words and all(w in c.db_name.split("-") for w in words)]
    return matches[0] if len(matches) == 1 else None


def _targets(campaigns: list, name: str, every: bool) -> list:
    """The campaigns a command runs on; exits listing the real names if none match."""
    if every:
        return campaigns
    found = _get_campaign(campaigns, name or "")
    if found is None:
        click.echo(f"No campaign matches '{name}'. Campaigns:", err=True)
        for c in campaigns:
            click.echo(f"  {c.db_name}", err=True)
        sys.exit(1)
    return [found]


@click.group()
@click.option("--db", default=lambda: os.environ.get("DATABASE_URL", ""),
              help="PostgreSQL connection URL (default: $DATABASE_URL)")
@click.pass_context
def cli(ctx, db):
    """agency-os — plugin-driven sales outreach engine."""
    ctx.ensure_object(dict)
    ctx.obj["db_url"] = db


@cli.command()
@click.option("--campaign", "campaign_name", help="Specific campaign to sync")
@click.option("--all", "sync_all", is_flag=True, help="Sync all campaigns")
@click.option("--dry-run", is_flag=True, help="Don't write to DB, just print")
@click.pass_context
def sync(ctx, campaign_name, sync_all, dry_run):
    """Sync prospects from all configured sources."""
    if not campaign_name and not sync_all:
        click.echo("Error: specify --campaign <name> or --all")
        sys.exit(1)

    registry, db, campaigns = _setup(db_url=ctx.obj["db_url"])
    pipeline = Pipeline(db, registry)

    targets = _targets(campaigns, campaign_name, sync_all)
    if not targets:
        click.echo(f"No campaigns found matching '{campaign_name}'")
        sys.exit(1)

    for campaign in targets:
        click.echo(f"\n{'='*60}")
        click.echo(f"Syncing: {campaign.name}")
        click.echo(f"{'='*60}")
        stats = pipeline.sync_prospects(campaign, dry_run=dry_run)
        click.echo(f"  Discovered: {stats['discovered']}")
        click.echo(f"  Upserted:   {stats['upserted']}")
        if stats["errors"]:
            click.echo(f"  Errors:     {stats['errors']}")


@cli.command()
@click.option("--campaign", "campaign_name", help="Specific campaign to enrich")
@click.option("--all", "enrich_all", is_flag=True, help="Enrich all campaigns")
@click.option("--limit", default=50, help="Max prospects to enrich")
@click.pass_context
def enrich(ctx, campaign_name, enrich_all, limit):
    """Enrich contact info for prospects missing email/name."""
    if not campaign_name and not enrich_all:
        click.echo("Error: specify --campaign <name> or --all")
        sys.exit(1)

    registry, db, campaigns = _setup(db_url=ctx.obj["db_url"])
    pipeline = Pipeline(db, registry)

    targets = _targets(campaigns, campaign_name, enrich_all)
    for campaign in targets:
        click.echo(f"\nEnriching: {campaign.name}")
        stats = pipeline.enrich_contacts(campaign, limit=limit)
        click.echo(f"  Checked:  {stats['checked']}")
        click.echo(f"  Enriched: {stats['enriched']}")


@cli.command()
@click.option("--campaign", "campaign_name", help="Backfill only prospects in this campaign")
@click.option("--all", "all_prospects", is_flag=True, help="Backfill every prospect with an EIN")
@click.option("--dry-run", is_flag=True, help="Print what would be updated without writing")
@click.pass_context
def backfill_irs_subsection(ctx, campaign_name, all_prospects, dry_run):
    """Backfill prospect.metadata.irs_subsection from IRS BMF.

    Re-downloads the IRS CA CSV once, builds an EIN→subsection map, and
    updates any prospect whose metadata lacks irs_subsection. Needed after
    the org_type pre-seed change (commit c741eb1) so existing prospects
    provision demos with the right org_type instead of the 'cbo' default.
    """
    import csv as _csv
    import io as _io
    import httpx as _httpx

    if not campaign_name and not all_prospects:
        click.echo("Error: specify --campaign <name> or --all")
        sys.exit(1)

    registry, db, campaigns = _setup(db_url=ctx.obj["db_url"])

    # Which campaigns to target — reuse the same matcher as every other command
    targets = _targets(campaigns, campaign_name, all_prospects)

    # Build EIN → subsection map from IRS BMF
    click.echo("Downloading IRS CA BMF (eo_ca.csv)…")
    from plugins.prospect_sources.irs_bmf import IrsBmfSource
    src = IrsBmfSource()
    with _httpx.Client(timeout=120, follow_redirects=True) as client:
        resp = client.get(src.URL)
        resp.raise_for_status()

    ein_to_sub: dict[str, str] = {}
    reader = _csv.DictReader(_io.StringIO(resp.text))
    for row in reader:
        ein = (row.get("EIN") or "").strip()
        sub = (row.get("SUBSECTION") or "").strip()
        if ein and sub:
            ein_to_sub[ein] = sub
    click.echo(f"  {len(ein_to_sub)} EINs have a subsection on file")

    # Walk prospects and patch metadata in place
    import json as _json
    updated = skipped = already = 0
    for campaign in targets:
        campaign_db_name = getattr(campaign, "db_name", campaign.name)
        cid = db.get_campaign_id(campaign_db_name)
        if cid is None:
            click.echo(f"\n{campaign_db_name}: no DB row yet, skipping (run sync first)")
            continue
        rows = db.conn.execute(
            "SELECT p.id, p.ein, p.metadata FROM prospects p "
            "JOIN outreach o ON o.prospect_id = p.id "
            "WHERE o.campaign_id = ? AND p.ein IS NOT NULL AND p.ein != ''",
            (cid,),
        ).fetchall()
        click.echo(f"\n{campaign_db_name}: {len(rows)} prospects with EIN")
        for r in rows:
            try:
                meta = _json.loads(r["metadata"] or "{}")
            except Exception:
                meta = {}
            if meta.get("irs_subsection"):
                already += 1
                continue
            sub = ein_to_sub.get(r["ein"])
            if not sub:
                skipped += 1
                continue
            meta["irs_subsection"] = sub
            if not dry_run:
                db.conn.execute(
                    "UPDATE prospects SET metadata = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                    (_json.dumps(meta), r["id"]),
                )
            updated += 1
        if not dry_run:
            db.conn.commit()

    click.echo(f"\n{'DRY RUN — ' if dry_run else ''}Updated: {updated}  Skipped (no match): {skipped}  Already set: {already}")


@cli.command()
@click.option("--campaign", "campaign_name", help="Specific campaign")
@click.option("--all", "all_campaigns", is_flag=True, help="Enqueue all campaigns")
@click.option("--limit", default=50, help="Max outreach emails per campaign")
@click.option("--dry-run", is_flag=True, help="Don't send, just print what would go out")
@click.option("--test-email", default="", help="Redirect ALL emails to this address instead of prospects' real emails")
@click.pass_context
def enqueue(ctx, campaign_name, all_campaigns, limit, dry_run, test_email):
    """Enqueue and send due follow-up emails.

    Use --test-email to redirect all outbound emails to a single address
    for testing the full pipeline without emailing real prospects.
    """
    if not campaign_name and not all_campaigns:
        click.echo("Error: specify --campaign <name> or --all")
        sys.exit(1)

    registry, db, campaigns = _setup(db_url=ctx.obj["db_url"])
    pipeline = Pipeline(db, registry)

    if test_email:
        click.echo(f"\n  ⚠ TEST MODE — all emails redirected to: {test_email}")

    targets = _targets(campaigns, campaign_name, all_campaigns)
    for campaign in targets:
        click.echo(f"\n{'='*60}")
        click.echo(f"Outreach: {campaign.name}")
        click.echo(f"{'='*60}")
        stats = pipeline.enqueue_outreach(campaign, limit=limit, dry_run=dry_run, test_email=test_email)
        click.echo(f"  Sent:       {stats['sent']}")
        click.echo(f"  Skipped:    {stats['skipped']}")
        click.echo(f"  Failed:     {stats['failed']}")
        click.echo(f"  No contact: {stats['no_contact']}")


@cli.command()
@click.option("--campaign", "campaign_name", required=True, help="Campaign slug")
@click.option("--to", "to_email", required=True, help="Email address to send the test to")
@click.option("--script", "script_name", default="00_cold_outreach", help="Script stem (e.g. 00_cold_outreach)")
@click.option("--prospect-id", type=int, default=0, help="Use a real prospect's data for personalization (0 = test data)")
@click.pass_context
def test_send(ctx, campaign_name, to_email, script_name, prospect_id):
    """Send a single test email to verify scripts and personalization.

    Uses real campaign scripts with either a real prospect's data (--prospect-id)
    or test placeholder data. The email goes to --to, not to the prospect.

    Examples:
      agency_os.py test-send --campaign <campaign> --to you@example.com
      agency_os.py test-send --campaign <campaign> --to you@example.com --script 01_followup_impact --prospect-id 1
    """
    registry, db, campaigns = _setup(db_url=ctx.obj["db_url"])
    pipeline = Pipeline(db, registry)

    campaign = _get_campaign(campaigns, campaign_name)
    if not campaign:
        click.echo(f"Error: campaign '{campaign_name}' not found")
        sys.exit(1)

    # Load the script
    try:
        script = campaign.load_script(script_name)
    except FileNotFoundError:
        click.echo(f"Error: script '{script_name}' not found in {campaign_name}")
        sys.exit(1)

    # Use a real prospect or create test data
    if prospect_id:
        prospect = db.get_prospect(prospect_id)
        if not prospect:
            click.echo(f"Error: prospect {prospect_id} not found")
            sys.exit(1)
        # Get outreach row for this prospect
        rows = db.conn.execute(
            "SELECT * FROM outreach WHERE prospect_id = ? AND campaign_id = (SELECT id FROM campaigns WHERE name = ?)",
            (prospect_id, campaign_name),
        ).fetchall()
        from core.models import Outreach as OutreachModel
        if rows:
            r = rows[0]
            outreach = OutreachModel(
                id=r["id"], prospect_id=r["prospect_id"], campaign_id=r["campaign_id"],
                stage=r["stage"], touch_count=r["touch_count"],
                contact_name=r["contact_name"], contact_email=r["contact_email"],
                contact_phone=r["contact_phone"], contact_title=r["contact_title"],
                script_variant=r["script_variant"],
                last_contacted_at=r["last_contacted_at"],
                next_follow_up_at=r["next_follow_up_at"],
                demo_link=r["demo_link"], notes=r["notes"],
                activity_log=[], assigned_to=r["assigned_to"],
                closed_at=r["closed_at"], close_reason=r["close_reason"],
            )
        else:
            outreach = None
        click.echo(f"  Using real prospect: {prospect.name} ({prospect.city}, {prospect.state})")
    else:
        from core.models import Prospect, Outreach
        prospect = Prospect(
            id=0,
            name="Test Community Organization",
            ein="",
            ntee_code="R",
            website_url="https://example.org",
            address="123 Main St",
            city="Los Angeles",
            state="CA",
            zip="90001",
            county="Los Angeles",
            focus_area="civic_engagement",
            annual_revenue=500000,
            voter_engagement=1,
            source="test",
            source_url="",
            metadata={},
        )
        from datetime import datetime
        outreach = Outreach(
            id=0,
            prospect_id=0,
            campaign_id=0,
            stage="cold",
            touch_count=0,
            contact_name="Test Contact",
            contact_email=to_email,
            contact_phone="",
            contact_title="Executive Director",
            script_variant="",
            last_contacted_at=None,
            next_follow_up_at=None,
            demo_link="https://www.u9itus.com/compare?state=ca",
            notes="",
            activity_log="[]",
            assigned_to="",
            closed_at=None,
            close_reason="",
        )
        click.echo(f"  Using test prospect: {prospect.name}")

    # Build variables and render
    if outreach is None:
        from core.models import Outreach as OutreachModel
        from datetime import datetime
        outreach = OutreachModel(
            id=0, prospect_id=prospect.id, campaign_id=0,
            stage="cold", touch_count=0,
            contact_name="", contact_email=to_email,
            contact_phone="", contact_title="",
            script_variant="",
            last_contacted_at=None, next_follow_up_at=None,
            demo_link="https://www.u9itus.com/compare?state=ca",
            notes="", activity_log=[], assigned_to="",
            closed_at=None, close_reason="",
        )

    variables = pipeline._build_variables(campaign, prospect, outreach)
    subject = pipeline._render(script.get("subject", ""), variables)
    body = pipeline._render(script.get("body", ""), variables)

    click.echo(f"\n  Script: {script_name}")
    click.echo(f"  Subject: {subject}")
    click.echo(f"  To: {to_email}")
    click.echo(f"\n  Body preview:\n  {body[:300]}...")

    # Send via the first configured channel
    sent = False
    for ch_key in campaign.channels:
        channel = registry.get_channel(ch_key)
        if not channel or not channel.is_configured():
            continue
        try:
            result = channel.send(
                recipient={"email": to_email, "phone": "", "name": outreach.contact_name or ""},
                subject=subject,
                body=body,
                metadata={
                    "campaign": campaign.db_name,
                    "outreach_id": 0,
                    "template_key": script.get("key", script_name),
                    "test": True,
                },
            )
            if result.status == "sent":
                click.echo(f"\n  ✓ Sent via {ch_key} (ID: {result.provider_message_id or 'n/a'})")
                sent = True
                break
            elif result.status == "skipped":
                click.echo(f"  · {ch_key} skipped: {result.error}")
            else:
                click.echo(f"  ✗ {ch_key} failed: {result.error}")
        except Exception as exc:
            click.echo(f"  ✗ {ch_key} error: {exc}")

    if not sent:
        click.echo("\n  ✗ No channel could send. Configure SMTP or Smartlead in .env")


@cli.command()
@click.option("--campaign", "campaign_name", help="Specific campaign")
@click.option("--all", "all_campaigns", is_flag=True, help="Provision for all campaigns")
@click.option("--limit", default=50, help="Max prospects to provision")
@click.option("--dry-run", is_flag=True, help="Show what would be provisioned without calling the API")
@click.pass_context
def provision(ctx, campaign_name, all_campaigns, limit, dry_run):
    """Provision personal demo portals for prospects with contact emails."""
    if not campaign_name and not all_campaigns:
        click.echo("Error: specify --campaign <name> or --all")
        sys.exit(1)

    registry, db, campaigns = _setup(db_url=ctx.obj["db_url"])
    pipeline = Pipeline(db, registry)

    targets = _targets(campaigns, campaign_name, all_campaigns)
    for campaign in targets:
        click.echo(f"\n{'='*60}")
        click.echo(f"Provisioning: {campaign.name}")
        click.echo(f"{'='*60}")
        stats = pipeline.provision_demos(campaign, limit=limit, dry_run=dry_run)
        click.echo(f"  Provisioned: {stats['provisioned']}")
        click.echo(f"  Skipped:     {stats['skipped']}")
        click.echo(f"  Failed:      {stats['failed']}")
        click.echo(f"  No contact:  {stats['no_contact']}")
        if stats.get("api_not_configured"):
            click.echo(f"\n  ⚠ Product API not configured. {stats['setup_hint']}")


@cli.command()
@click.option("--campaign", "campaign_name", help="Specific campaign")
@click.option("--all", "all_campaigns", is_flag=True, help="Pull events for all campaigns")
@click.option("--dry-run", is_flag=True, help="Show events without updating the pipeline")
@click.pass_context
def pull_events(ctx, campaign_name, all_campaigns, dry_run):
    """Pull demo portal events from the product and auto-advance pipeline stages."""
    if not campaign_name and not all_campaigns:
        click.echo("Error: specify --campaign <name> or --all")
        sys.exit(1)

    registry, db, campaigns = _setup(db_url=ctx.obj["db_url"])
    pipeline = Pipeline(db, registry)

    targets = _targets(campaigns, campaign_name, all_campaigns)
    for campaign in targets:
        click.echo(f"\n{'='*60}")
        click.echo(f"Pulling events: {campaign.name}")
        click.echo(f"{'='*60}")
        stats = pipeline.pull_product_events(campaign, dry_run=dry_run)
        click.echo(f"  Events pulled:    {stats['events_pulled']}")
        click.echo(f"  Stage changes:    {stats['stage_changes']}")
        click.echo(f"  Already processed:{stats['already_processed']}")
        if stats.get("api_not_configured"):
            click.echo(f"\n  ⚠ Product API not configured. {stats['setup_hint']}")


@cli.command()
@click.option("--campaign", "campaign_name", help="Specific campaign")
@click.option("--all", "all_campaigns", is_flag=True, help="Check all campaigns")
@click.pass_context
def stale(ctx, campaign_name, all_campaigns):
    """Move stale prospects to nurture stage."""
    if not campaign_name and not all_campaigns:
        click.echo("Error: specify --campaign <name> or --all")
        sys.exit(1)

    registry, db, campaigns = _setup(db_url=ctx.obj["db_url"])
    pipeline = Pipeline(db, registry)

    targets = _targets(campaigns, campaign_name, all_campaigns)
    for campaign in targets:
        click.echo(f"\nStale check: {campaign.name}")
        stats = pipeline.check_stale(campaign)
        click.echo(f"  Moved to nurture: {stats['moved_to_nurture']}")


@cli.command()
@click.option("--campaign", "campaign_name", help="Specific campaign")
@click.option("--all", "all_campaigns", is_flag=True, help="Sync all campaigns with a scheduler")
@click.option("--days", default=30, help="Look back this many days for bookings")
@click.option("--dry-run", is_flag=True, help="Show matches without updating the pipeline")
@click.pass_context
def bookings(ctx, campaign_name, all_campaigns, days, dry_run):
    """Sync booked meetings (e.g. Calendly) into the pipeline."""
    if not campaign_name and not all_campaigns:
        click.echo("Error: specify --campaign <name> or --all")
        sys.exit(1)

    registry, db, campaigns = _setup(db_url=ctx.obj["db_url"])
    pipeline = Pipeline(db, registry)

    targets = _targets(campaigns, campaign_name, all_campaigns)
    for campaign in targets:
        if not campaign.scheduler:
            if not all_campaigns:
                click.echo(f"\n{campaign.name}: no scheduler set in campaign.yaml")
            continue
        click.echo(f"\nBooking sync: {campaign.name} (via {campaign.scheduler})")
        stats = pipeline.sync_bookings(campaign, days_back=days, dry_run=dry_run)
        if stats.get("error"):
            click.echo(f"  ! {stats['error']}")
            continue
        click.echo(f"  Booked:    {stats['booked']}")
        click.echo(f"  Canceled:  {stats['canceled']}")
        click.echo(f"  Unmatched: {stats['unmatched']}")
        click.echo(f"  Unchanged: {stats['unchanged']}")


@cli.command()
@click.option("--campaign", "campaign_name", help="Specific campaign")
@click.option("--all", "all_campaigns", is_flag=True, help="Digest all campaigns")
@click.pass_context
def digest(ctx, campaign_name, all_campaigns):
    """Show pipeline digest for a campaign."""
    if not campaign_name and not all_campaigns:
        click.echo("Error: specify --campaign <name> or --all")
        sys.exit(1)

    registry, db, campaigns = _setup(db_url=ctx.obj["db_url"])
    pipeline = Pipeline(db, registry)

    targets = _targets(campaigns, campaign_name, all_campaigns)
    for campaign in targets:
        click.echo(f"\n{'='*60}")
        click.echo(f"Digest: {campaign.name}")
        click.echo(f"{'='*60}")
        stats = pipeline.weekly_digest(campaign)
        click.echo(f"  Total prospects:  {stats['total_prospects']}")
        click.echo(f"  Emails sent:      {stats['total_emails_sent']}")
        click.echo(f"  Open rate:        {stats['open_rate']}")
        click.echo(f"  Reply rate:       {stats['reply_rate']}")
        click.echo("  Stage breakdown:")
        for stage, count in sorted(stats["stage_counts"].items()):
            click.echo(f"    {stage:20s} {count}")
        if "spend" in stats:
            from core.payments import atomic_to_usd

            spent = stats["spend"]
            click.echo(f"  Lead packages:    ${atomic_to_usd(spent['month_atomic']):,.2f} this month, "
                       f"${atomic_to_usd(spent['total_atomic']):,.2f} all time"
                       + (f", {spent['pending']} pending" if spent["pending"] else ""))


@cli.command()
@click.pass_context
def campaigns(ctx):
    """List all discovered campaigns."""
    registry, db, all_campaigns = _setup(db_url=ctx.obj["db_url"])
    click.echo(f"\nActive campaigns ({len(all_campaigns)}):")
    for c in all_campaigns:
        click.echo(f"  {c.db_name:30s} product={c.product}")
        click.echo(f"    sources: {', '.join(c.prospect_sources)}")
        click.echo(f"    channels: {', '.join(c.channels)}")


@cli.command()
@click.option("--type", "plugin_type", type=click.Choice(["prospect_sources", "products", "channels", "enrichers", "schedulers", "all"]), default="all")
@click.pass_context
def plugins(ctx, plugin_type):
    """List all discovered plugins."""
    registry, db, all_campaigns = _setup(db_url=ctx.obj["db_url"])
    all_plugins = registry.list_plugins()
    if plugin_type == "all":
        for ptype, keys in all_plugins.items():
            click.echo(f"\n{ptype}:")
            for k in keys:
                click.echo(f"  {k}")
    else:
        keys = all_plugins.get(plugin_type, [])
        click.echo(f"\n{plugin_type}:")
        for k in keys:
            click.echo(f"  {k}")


@cli.command("new-plugin")
@click.argument("name")
@click.option("--title", default="", help='Shown in the nav and on the page (default: from the name)')
def new_plugin(name, title):
    """Create a plugin with every part wired together: a page, a prospect-page panel,
    a prospect source, a scheduled AI job, an agent and a test (see docs/PLUGINS.md)."""
    from core import scaffold

    try:
        written = scaffold.create(name, title)
    except scaffold.ScaffoldError as exc:
        click.echo(f"Error: {exc}")
        sys.exit(1)
    values = scaffold.names(name, title)
    click.echo(f"Created the {values['title']} plugin:")
    for path in written:
        click.echo(f"  {path.relative_to(scaffold.PROJECT_ROOT)}")
    click.echo(f"""
Next:
  1. python -m pytest tests/test_plugin_{values['module']}.py
  2. Restart the app: the page is at /p/{values['key']} (More menu), the job is on Administration -> Jobs,
     and its panel and agent are on every prospect page.
  3. Point the source at your data with {values['ENV']}_SOURCE_URL, and add `- {values['module']}`
     under prospect_sources in a campaign.yaml.""")


@cli.command("import-sqlite")
@click.option("--from", "sqlite_path", required=True, type=click.Path(exists=True, dir_okay=False),
              help="SQLite database file from before the PostgreSQL move (e.g. db.sqlite)")
@click.pass_context
def import_sqlite_cmd(ctx, sqlite_path):
    """Copy an old SQLite database into an empty PostgreSQL database (one time)."""
    from core.migrate import import_sqlite

    db = Database(ctx.obj["db_url"] or None)
    click.echo(f"Importing {sqlite_path} into {redact_url(db.url)}...")
    try:
        copied, skipped = import_sqlite(sqlite_path, db)
    except RuntimeError as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)
    for table, count in copied.items():
        note = f" (skipped {skipped[table]} pointing at deleted rows)" if table in skipped else ""
        click.echo(f"  {table}: {count}{note}")
    click.echo("Done.")


# ── Users ──────────────────────────────────────────────────────────────
# Day-to-day user and role management happens in the web UI (/admin/users).
# These commands cover bootstrap and recovery, so they run as "cli" in the
# audit log and need shell access to the server instead of a login.


def _access_db(ctx) -> Database:
    db = Database(ctx.obj["db_url"] or None)
    db.install_access()
    return db


def _fail(e: AccessError):
    click.echo(f"Error: {e}", err=True)
    sys.exit(1)


@cli.group()
def users():
    """Manage dashboard users (bootstrap & recovery)."""


@users.command("list")
@click.pass_context
def users_list(ctx):
    """List users and their roles."""
    db = _access_db(ctx)
    role_names = {r["id"]: r["name"] for r in db.list_roles()}
    for u in db.list_users():
        roles = ", ".join(sorted(role_names[r] for r in u["role_ids"])) or "(no roles)"
        status = "" if u["is_active"] else "  [deactivated]"
        click.echo(f"  {u['email']:<36} {u['name']:<24} {roles}{status}")


@users.command("create-owner")
@click.option("--email", required=True)
@click.option("--name", default="", help="Display name (defaults to the email's local part)")
@click.password_option(help="Password (prompted if omitted)")
@click.pass_context
def users_create_owner(ctx, email, name, password):
    """Create a new user with the Owner role."""
    db = _access_db(ctx)
    owner_id = next(r["id"] for r in db.list_roles() if r["name"] == OWNER_ROLE)
    try:
        db.create_user(email, name or email.split("@")[0], password, [owner_id], actor=None)
    except AccessError as e:
        _fail(e)
    click.echo(f"Created owner {email.strip().lower()}")


@users.command("grant-owner")
@click.option("--email", required=True)
@click.pass_context
def users_grant_owner(ctx, email):
    """Recovery: give an existing user the Owner role and reactivate them."""
    try:
        _access_db(ctx).grant_owner(email, actor=None)
    except AccessError as e:
        _fail(e)
    click.echo(f"{email} is now an active owner")


@users.command("grant-super-admin")
@click.option("--email", required=True)
@click.pass_context
def users_grant_super_admin(ctx, email):
    """Bootstrap/recovery: make an existing user an active Super Admin (can create and promote Owners)."""
    try:
        _access_db(ctx).grant_owner(email, actor=None, role=SUPER_ADMIN_ROLE)
    except AccessError as e:
        _fail(e)
    click.echo(f"{email} is now an active Super Admin")


@users.command("set-password")
@click.option("--email", required=True)
@click.password_option(help="New password (prompted if omitted)")
@click.pass_context
def users_set_password(ctx, email, password):
    """Recovery: set a user's password and sign out their sessions."""
    db = _access_db(ctx)
    row = db.get_user_by_email(email)
    if not row:
        _fail(AccessError(f"No user with email {email}."))
    try:
        db.set_password(row["id"], password, actor=None)
    except AccessError as e:
        _fail(e)
    click.echo(f"Password updated for {row['email']}")


@users.command("invite")
@click.option("--email", required=True)
@click.option("--name", default="", help="Display name (new users; defaults to the email's local part)")
@click.option("--role", "role_names", multiple=True,
              help="Role for a new user; repeat for several (e.g. --role Caller)")
@click.option("--base-url", default="",
              help="Dashboard URL for the link (default: $AGENCY_OS_BASE_URL or Railway's domain)")
@click.option("--no-send", is_flag=True, help="Print the email instead of sending it")
@click.pass_context
def users_invite(ctx, email, name, role_names, base_url, no_send):
    """Send a welcome email with a one-time set-password link.

    Creates the user if they don't exist yet. For an existing user it sends a
    fresh link (their roles are left alone; change those in Team → Users).
    Any earlier unused link for the user stops working.
    """
    from core import welcome

    site_url = (base_url or welcome.base_url()).rstrip("/")
    if not site_url:
        _fail(AccessError("No dashboard URL. Pass --base-url https://... or set AGENCY_OS_BASE_URL."))
    if not no_send and not welcome.smtp_configured():
        _fail(AccessError("SMTP isn't configured (SMTP_HOST, SMTP_USER, SMTP_PASS). "
                          "Set it up, or use --no-send to print the email and send it yourself."))

    db = _access_db(ctx)
    row = db.get_user_by_email(email)
    if row:
        if role_names:
            click.echo(f"{row['email']} already exists; ignoring --role (edit roles in Team → Users).")
        user_id = row["id"]
    else:
        roles_by_name = {r["name"].lower(): r["id"] for r in db.list_roles()}
        unknown = [r for r in role_names if r.lower() not in roles_by_name]
        if unknown:
            _fail(AccessError(f"Unknown role(s): {', '.join(unknown)}. "
                              f"Choose from: {', '.join(r['name'] for r in db.list_roles())}"))
        # Random throwaway password: nobody can sign in until the link is used.
        try:
            user_id = db.create_user(
                email, name or email.split("@")[0], access.new_session_token(),
                [roles_by_name[r.lower()] for r in role_names], actor=None,
            )
        except AccessError as e:
            _fail(e)
        click.echo(f"Created {email.strip().lower()}"
                   + ("" if role_names else " with no roles (they'll see only their account page)"))

    try:
        link, expires_at = welcome.issue_invite(db, user_id, site_url)
    except AccessError as e:
        _fail(e)
    user = db.load_current_user(user_id)
    subject, body = welcome.compose(user, link, expires_at, site_url)

    if no_send:
        click.echo(f"\nTo: {user.email}\nSubject: {subject}\n\n{body}")
        return
    result = welcome.send(user.email, subject, body)
    if result.status != "sent":
        click.echo(f"Email not sent ({result.error}). Share this link with them instead:\n  {link}", err=True)
        sys.exit(1)
    click.echo(f"Welcome email sent to {user.email} (link expires {expires_at:%b %d, %Y})")


# ── Customer accounts (u9itus billing) ──────────────────────────────


@cli.group("accounts")
def accounts_group():
    """Customer accounts that use the /api/v1 search API (docs/U9ITUS_BILLING.md)."""


@accounts_group.command("platform-key")
def accounts_platform_key():
    """Make a platform key for u9itus. Prints the key (for u9itus) and its hash (for agency-os)."""
    from core import accounts

    key, digest = accounts.new_platform_key()
    click.echo("Give this key to u9itus (AGENCY_OS_PLATFORM_KEY). It is shown once:")
    click.echo(f"  {key}")
    click.echo("Set this on agency-os; it replaces any earlier platform key:")
    click.echo(f"  AGENCY_OS_PLATFORM_KEY_HASH={digest}")


@accounts_group.command("list")
@click.pass_context
def accounts_list(ctx):
    """List accounts, their status and how many prospects they have."""
    from core import accounts

    rows = accounts.list_accounts(Database(ctx.obj["db_url"] or None))
    if not rows:
        click.echo("No accounts.")
    for a in rows:
        click.echo(f"  {a['external_ref']:<24} {a['name']:<32} {a['status']:<10} "
                   f"{a['prospects']:>6} prospects  key ...{a['key_hint'] or ''}")


@accounts_group.command("create")
@click.option("--ref", "external_ref", required=True, help="u9itus's id for the customer")
@click.option("--name", required=True)
@click.pass_context
def accounts_create(ctx, external_ref, name):
    """Make an account by hand (u9itus normally does this through the API)."""
    from core import accounts

    try:
        account, key = accounts.create(Database(ctx.obj["db_url"] or None), external_ref, name)
    except accounts.AccountError as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)
    if key is None:
        click.echo(f"{account['external_ref']} already exists; use `accounts rotate-key` for a new key.")
        return
    click.echo(f"Created {account['external_ref']}. Its key, shown once:\n  {key}")


@accounts_group.command("rotate-key")
@click.option("--ref", "external_ref", required=True)
@click.pass_context
def accounts_rotate_key(ctx, external_ref):
    """Issue a new key for an account; the old one stops working."""
    from core import accounts

    try:
        key = accounts.rotate_key(Database(ctx.obj["db_url"] or None), external_ref)
    except accounts.AccountError as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)
    click.echo(f"New key for {external_ref}, shown once:\n  {key}")


@accounts_group.command("set-status")
@click.option("--ref", "external_ref", required=True)
@click.option("--status", type=click.Choice(["active", "suspended"]), required=True)
@click.pass_context
def accounts_set_status(ctx, external_ref, status):
    """Suspend an account or make it active again."""
    from core import accounts

    try:
        accounts.set_status(Database(ctx.obj["db_url"] or None), external_ref, status)
    except accounts.AccountError as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)
    click.echo(f"{external_ref} is now {status}.")


@cli.group("searches")
def searches_group():
    """Paid account searches (docs/U9ITUS_BILLING.md)."""


@searches_group.command("run")
@click.option("--limit", default=20, show_default=True, help="Most searches to run")
@click.pass_context
def searches_run(ctx, limit):
    """Run queued searches now (the web app does this itself with AGENCY_OS_RUN_SEARCHES=1)."""
    from core import searches

    finished = searches.SearchRunner(ctx.obj["db_url"] or None).run_pending(limit=limit)
    if not finished:
        click.echo("No queued searches.")
    for s in finished:
        click.echo(f"  #{s['id']:<6} {s['type']:<7} {s['status']:<9} delivered {s['delivered']}/{s['max_results']}"
                   + (f"  ({s['error']})" if s["error"] else ""))


@cli.group("generator")
def generator_group():
    """Lead package generator runs (core/generator.py). Super Admins start them at /admin/generator."""


@generator_group.command("run")
@click.option("--id", "run_id", type=int, default=None, help="Run this queued run (default: every queued run)")
@click.pass_context
def generator_run(ctx, run_id):
    """Run queued generator runs now, e.g. one left queued by a restart."""
    from core import generator

    runner = generator.Runner(ctx.obj["db_url"] or None)
    finished = [r for r in [runner.run(run_id)] if r] if run_id else runner.run_pending()
    if not finished:
        click.echo("No queued generator runs.")
    for r in finished:
        click.echo(f"  #{r['id']:<6} {r['status']:<9} found {r['found']}/{r['max_leads']}, "
                   f"{r['enriched']} with a contact, {r['eligible']} sellable"
                   + (f"  ({r['error']})" if r["error"] else ""))


# ── Lead packages (x402) ───────────────────────────────────────────


@cli.group()
def packages():
    """Browse and unlock x402 lead packages."""


@packages.command("list")
def packages_list():
    """List packages from the providers in AGENCY_OS_LEAD_PROVIDERS."""
    from core import lead_packages

    providers = lead_packages.configured_providers()
    if not providers:
        click.echo("No providers configured (AGENCY_OS_LEAD_PROVIDERS).")
        return
    for provider in providers:
        found, error = lead_packages.fetch_catalog(provider)
        click.echo(f"\n{provider}" + (f"  ! {error}" if error else ""))
        for p in found:
            badge = "90% guarantee" if p.guaranteed else "no guarantee"
            click.echo(f"  {p.package_id:<24} {p.title[:40]:<40} {p.lead_count:>6} leads  "
                       f"unlock ${p.unlock_usd:,.2f}  royalty ${p.royalty_usd:,.2f}  [{badge}]")


@packages.command("unlock")
@click.option("--campaign", "campaign_name", required=True)
@click.option("--provider", required=True)
@click.option("--package", "package_id", required=True)
@click.option("--email", required=True, help="The user paying (their allowance applies)")
@click.option("--yes", is_flag=True, help="Skip the confirmation prompt")
@click.option("--dry-run", is_flag=True, help="Show the cost and budgets left; don't pay")
@click.pass_context
def packages_unlock(ctx, campaign_name, provider, package_id, email, yes, dry_run):
    """Pay for a package and import its leads into a campaign."""
    from core import lead_packages
    from core.payments import atomic_to_usd

    _registry, db, campaigns = _setup(db_url=ctx.obj["db_url"])
    campaign = _get_campaign(campaigns, campaign_name)
    if not campaign:
        _fail(AccessError(f"Campaign not found: {campaign_name}"))
    row = db.get_user_by_email(email)
    user = db.load_current_user(row["id"]) if row else None
    if user is None or not user.can("packages.buy"):
        _fail(AccessError(f"{email} can't buy packages (needs the packages.buy permission)"))
    package, error = lead_packages.find_package(lead_packages.normalize_provider(provider) or "", package_id)
    if package is None:
        _fail(AccessError(error))
    campaign_id = db.upsert_campaign(campaign.db_name, str(campaign.config_dir / "campaign.yaml"))
    preview = lead_packages.preview_unlock(db, campaign, campaign_id, user, package)
    click.echo(f"{package.title}: unlock ${atomic_to_usd(preview.unlock_atomic):,.2f} now, royalties up to "
               f"${atomic_to_usd(preview.max_royalties_atomic):,.2f} if every lead is contacted")
    click.echo(f"Left this month: campaign ${atomic_to_usd(preview.campaign_left_atomic):,.2f}, "
               f"you ${atomic_to_usd(preview.allowance_left_atomic):,.2f}")
    for problem in preview.problems:
        click.echo(f"  ! {problem}")
    if preview.already_unlocked:
        click.echo("Already unlocked for this campaign.")
        return
    if dry_run:
        sys.exit(0 if preview.ok else 1)
    if not yes:
        click.confirm("Pay and unlock?", abort=True)
    result = lead_packages.unlock(db, campaign, campaign_id, user, package)
    click.echo(result.message)
    if not result.ok:
        sys.exit(1)


@packages.command("verify")
@click.option("--campaign", "campaign_name", required=True)
@click.option("--ai", is_flag=True, help="Include the AI review (AGENCY_OS_AI_REVIEW=on, ANTHROPIC_API_KEY)")
@click.pass_context
def packages_verify(ctx, campaign_name, ai):
    """Re-check a campaign's package leads against the 90% guarantee."""
    from core import claims, verify

    _registry, db, campaigns = _setup(db_url=ctx.obj["db_url"])
    campaign = _get_campaign(campaigns, campaign_name)
    campaign_id = db.get_campaign_id(campaign.db_name) if campaign else None
    if not campaign_id:
        _fail(AccessError(f"Campaign not found: {campaign_name}"))
    reviewer = verify.default_reviewer() if ai else None
    if ai and not reviewer.is_configured():
        click.echo("AI review is off (needs AGENCY_OS_AI_REVIEW=on and ANTHROPIC_API_KEY); scoring without it.")
    for lp in db.list_lead_packages(campaign_id):
        status = claims.package_status(db, lp["id"], reviewer=reviewer)
        s = status["summary"]
        rate = f"{s['rate']:.0%}" if s["rate"] is not None else "not worked yet"
        click.echo(f"#{lp['id']} {lp['title']}: {rate} verified ({s['verified']} verified, {s['failed']} failed, "
                   f"{s['unworked']} unworked of {s['lead_count']}); "
                   f"window {'open, ' + str(status['days_left']) + ' days left' if s['window_open'] else 'closed'}"
                   f"{'' if s['window_open'] or not status['window']['claims_open'] else ' (claims still accepted)'}")
        if s["claimable"]:
            click.echo(f"  ! {s['claimable']} lead(s) claimable: agency-os packages claim --id {lp['id']} --email ...")


@packages.command("claim")
@click.option("--id", "lead_package_id", type=int, required=True, help="Unlocked package id (from packages verify)")
@click.option("--email", required=True, help="The user filing the claim")
@click.option("--yes", is_flag=True, help="Skip the confirmation prompt")
@click.pass_context
def packages_claim(ctx, lead_package_id, email, yes):
    """File a guarantee claim with the provider for failed leads."""
    from core import claims

    db = _access_db(ctx)
    row = db.get_user_by_email(email)
    user = db.load_current_user(row["id"]) if row else None
    if user is None or not user.can("packages.buy"):
        _fail(AccessError(f"{email} can't file claims (needs the packages.buy permission)"))
    status = claims.package_status(db, lead_package_id)
    if status is None:
        _fail(AccessError(f"No unlocked package #{lead_package_id}"))
    if status["claim_problem"]:
        _fail(AccessError(status["claim_problem"]))
    if not yes:
        click.confirm(f"Claim {status['summary']['claimable']} lead(s) from {status['lp']['provider']}?", abort=True)
    result = claims.file_claim(db, lead_package_id, user)
    click.echo(result.message)
    if not result.ok:
        sys.exit(1)


@cli.group(invoke_without_command=True)
@click.option("--campaign", "campaign_name", help="Show one campaign's spending")
@click.pass_context
def spend(ctx, campaign_name):
    """Lead-package spending per campaign."""
    if ctx.invoked_subcommand:
        return
    from core.payments import SpendPolicy, atomic_to_usd

    _registry, db, campaigns = _setup(db_url=ctx.obj["db_url"])
    selected = [c for c in campaigns if not campaign_name or c is _get_campaign(campaigns, campaign_name)]
    for c in selected:
        campaign_id = db.get_campaign_id(c.db_name)
        if not campaign_id:
            continue
        summary = db.campaign_spend(campaign_id)
        policy = SpendPolicy.from_config(c.lead_packages)
        click.echo(f"\n{c.name}  ({'on' if policy.enabled else 'off'}, {policy.network})")
        click.echo(f"  this month: ${atomic_to_usd(summary['month_atomic']):,.2f}"
                   f" of ${atomic_to_usd(policy.monthly_budget_atomic):,.2f}")
        for kind, v in summary["by_kind"].items():
            click.echo(f"  {kind:<8} {v['count']:>5}  ${atomic_to_usd(v['total_atomic']):,.2f}")
        if summary["pending"]:
            click.echo(f"  ! {summary['pending']} payment(s) still pending")


@spend.command("allowance")
@click.option("--email", required=True)
@click.option("--usd", type=float, required=True, help="Monthly allowance in dollars (0 = can't spend)")
@click.pass_context
def spend_allowance(ctx, email, usd):
    """Set how much a user may spend on lead packages per month."""
    from core.payments import usd_to_atomic

    db = _access_db(ctx)
    row = db.get_user_by_email(email)
    if not row:
        _fail(AccessError(f"No user with email {email}"))
    db.set_spend_allowance(row["id"], usd_to_atomic(usd), actor=None)
    click.echo(f"{email} may now spend ${max(usd, 0):,.2f} per month on lead packages")


if __name__ == "__main__":
    cli()


@spend.command("pending")
@click.pass_context
def spend_pending(ctx):
    """Payments sent with no settlement read back. Check each onchain, then resolve it."""
    from core.payments import atomic_to_usd

    db = _access_db(ctx)
    rows = db.pending_spend()
    if not rows:
        click.echo("No pending payments.")
    for r in rows:
        click.echo(f"  #{r['id']:<6} {r['kind']:<8} ${atomic_to_usd(r['amount_atomic']):,.2f}  "
                   f"{r['campaign_name'] or '-'}  to {r['pay_to']} on {r['network']}  {r['created_at']}")
        if r["error"]:
            click.echo(f"          {r['error']}")


@spend.command("resolve")
@click.option("--id", "spend_id", type=int, required=True)
@click.option("--status", type=click.Choice(["settled", "failed"]), required=True)
@click.option("--tx", "tx_hash", default="", help="The onchain transaction hash (needed for settled)")
@click.pass_context
def spend_resolve(ctx, spend_id, status, tx_hash):
    """Close a pending payment. 'failed' frees its budget and lets it be paid again."""
    db = _access_db(ctx)
    try:
        resolved = db.resolve_spend(spend_id, status, tx_hash, actor=None)
    except ValueError as exc:
        _fail(AccessError(str(exc)))
    if not resolved:
        _fail(AccessError(f"Payment #{spend_id} isn't pending"))
    click.echo(f"Payment #{spend_id} marked {status}")


# ── Remote console ─────────────────────────────────────────────────────
# Runs console commands (core/console.py) on a deployed agency-os as you,
# with a CLI key from Account → Command console. No database access needed.

REMOTE_CONFIG = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "agency-os" / "cli.json"


def _remote_settings() -> tuple[str, str]:
    saved = {}
    try:
        saved = json.loads(REMOTE_CONFIG.read_text())
    except (OSError, ValueError):
        pass
    url = os.environ.get("AGENCY_OS_URL") or saved.get("url", "")
    key = os.environ.get("AGENCY_OS_KEY") or saved.get("key", "")
    if not url or not key:
        _fail(AccessError("Not connected. Run: agency_os.py connect --url https://... --key aos_cli_... "
                          "(create a key on your Account page)."))
    return url.rstrip("/"), key


def _remote_call(url: str, key: str, line: str, confirmed: bool = False) -> dict:
    import urllib.error
    import urllib.request

    request = urllib.request.Request(
        f"{url}/api/console", method="POST",
        data=json.dumps({"line": line, "confirmed": confirmed}).encode(),
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as e:
        if e.code == 401:
            return {"ok": False, "output": "Key not accepted (revoked, or your account was deactivated). "
                                           "Create a new one on your Account page."}
        if e.code == 403:
            return {"ok": False, "output": "Your account doesn't include the command console (cli.use). Ask an owner."}
        return {"ok": False, "output": f"agency-os answered HTTP {e.code}."}
    except (urllib.error.URLError, OSError, ValueError) as e:
        return {"ok": False, "output": f"Couldn't reach {url}: {e}"}


def _remote_run(url: str, key: str, line: str, yes: bool) -> bool:
    result = _remote_call(url, key, line)
    if result.get("needs_confirmation"):
        click.echo(result.get("output", ""))
        if not (yes or click.confirm("Run this?", default=False)):
            click.echo("Cancelled.")
            return False
        result = _remote_call(url, key, line, confirmed=True)
    if result.get("output"):
        click.echo(result["output"], err=not result.get("ok"))
    return bool(result.get("ok"))


@cli.command()
@click.option("--url", required=True, help="Your agency-os dashboard, e.g. https://your-app.up.railway.app")
@click.option("--key", required=True, help="A CLI key (aos_cli_...) from Account → Command console")
def connect(url, key):
    """Save the server and CLI key used by `remote`."""
    url = url.rstrip("/")
    result = _remote_call(url, key, "whoami")
    if not result.get("ok"):
        _fail(AccessError(result.get("output", "Couldn't connect.")))
    REMOTE_CONFIG.parent.mkdir(parents=True, exist_ok=True)
    REMOTE_CONFIG.touch(mode=0o600, exist_ok=True)
    REMOTE_CONFIG.chmod(0o600)
    REMOTE_CONFIG.write_text(json.dumps({"url": url, "key": key}))
    click.echo(f"Connected to {url} as {result['output'].splitlines()[0]}")


@cli.command(context_settings={"ignore_unknown_options": True, "allow_interspersed_args": False})
@click.option("--yes", "-y", is_flag=True, help="Don't ask before running a change")
@click.argument("words", nargs=-1, type=click.UNPROCESSED)
def remote(yes, words):
    """Run console commands on your agency-os server (none given: interactive).

    \b
    Examples:
      agency_os.py remote help
      agency_os.py remote users invite --email jane@example.com --name "Jane" --role Caller
      agency_os.py remote
    """
    import shlex

    url, key = _remote_settings()
    if words:
        sys.exit(0 if _remote_run(url, key, shlex.join(words), yes) else 1)
    try:
        import readline  # noqa: F401  (history and line editing for input())
    except ImportError:
        pass
    click.echo(f"agency-os at {url}. Type help, or exit to quit.")
    while True:
        try:
            line = input("agency-os $ ").strip()
        except (EOFError, KeyboardInterrupt):
            click.echo()
            return
        if line in ("exit", "quit"):
            return
        if line:
            _remote_run(url, key, line, yes)
