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
    agency-os users list
    agency-os users create-owner --email you@example.com
    agency-os users grant-owner --email someone@example.com
    agency-os users set-password --email someone@example.com
    agency-os users invite --email rep@example.com --name "Jane Rep" --role "Sales Rep"
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import click

from core import access
from core.access import AccessError, OWNER_ROLE
from core.campaign import discover_campaigns
from core.db import Database
from core.pipeline import Pipeline
from core.registry import PluginRegistry


def _setup(campaigns_dir: str = "campaigns", plugins_dir: str = "plugins", db_path: str = "db.sqlite"):
    """Initialize registry + DB, discover plugins + campaigns."""
    registry = PluginRegistry()
    print("Discovering plugins...")
    registry.discover(plugins_dir)
    db = Database(db_path)
    print(f"Database: {db_path}")
    print("Discovering campaigns...")
    campaigns = discover_campaigns(campaigns_dir)
    for c in campaigns:
        print(f"  + {c.db_name}")
    return registry, db, campaigns


def _get_campaign(campaigns: list, name: str):
    """Find a campaign by name (case-insensitive slug match)."""
    name_slug = name.lower().replace(" ", "-").replace("_", "-")
    for c in campaigns:
        if c.db_name == name_slug or c.name.lower() == name.lower():
            return c
    return None


@click.group()
@click.option("--db", default=lambda: os.environ.get("AGENCY_OS_DB", "db.sqlite"),
              help="Path to SQLite database file (default: $AGENCY_OS_DB or db.sqlite)")
@click.pass_context
def cli(ctx, db):
    """agency-os — plugin-driven sales outreach engine."""
    ctx.ensure_object(dict)
    ctx.obj["db_path"] = db


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

    registry, db, campaigns = _setup(db_path=ctx.obj["db_path"])
    pipeline = Pipeline(db, registry)

    targets = campaigns if sync_all else [c for c in campaigns if c.db_name == campaign_name]
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

    registry, db, campaigns = _setup(db_path=ctx.obj["db_path"])
    pipeline = Pipeline(db, registry)

    targets = campaigns if enrich_all else [c for c in campaigns if c.db_name == campaign_name]
    for campaign in targets:
        click.echo(f"\nEnriching: {campaign.name}")
        stats = pipeline.enrich_contacts(campaign, limit=limit)
        click.echo(f"  Checked:  {stats['checked']}")
        click.echo(f"  Enriched: {stats['enriched']}")


@cli.command()
@click.option("--campaign", "campaign_name", help="Specific campaign")
@click.option("--all", "all_campaigns", is_flag=True, help="Enqueue all campaigns")
@click.option("--limit", default=50, help="Max outreach emails per campaign")
@click.option("--dry-run", is_flag=True, help="Don't send, just print what would go out")
@click.pass_context
def enqueue(ctx, campaign_name, all_campaigns, limit, dry_run):
    """Enqueue and send due follow-up emails."""
    if not campaign_name and not all_campaigns:
        click.echo("Error: specify --campaign <name> or --all")
        sys.exit(1)

    registry, db, campaigns = _setup(db_path=ctx.obj["db_path"])
    pipeline = Pipeline(db, registry)

    targets = campaigns if all_campaigns else [c for c in campaigns if c.db_name == campaign_name]
    for campaign in targets:
        click.echo(f"\n{'='*60}")
        click.echo(f"Outreach: {campaign.name}")
        click.echo(f"{'='*60}")
        stats = pipeline.enqueue_outreach(campaign, limit=limit, dry_run=dry_run)
        click.echo(f"  Sent:       {stats['sent']}")
        click.echo(f"  Skipped:    {stats['skipped']}")
        click.echo(f"  Failed:     {stats['failed']}")
        click.echo(f"  No contact: {stats['no_contact']}")


@cli.command()
@click.option("--campaign", "campaign_name", help="Specific campaign")
@click.option("--all", "all_campaigns", is_flag=True, help="Provision for all campaigns")
@click.option("--limit", default=50, help="Max prospects to provision")
@click.option("--dry-run", is_flag=True, help="Show what would be provisioned without calling the API")
@click.pass_context
def provision(ctx, campaign_name, all_campaigns, limit, dry_run):
    """Provision personal demo portals on u9itus for prospects with contact emails."""
    if not campaign_name and not all_campaigns:
        click.echo("Error: specify --campaign <name> or --all")
        sys.exit(1)

    registry, db, campaigns = _setup(db_path=ctx.obj["db_path"])
    pipeline = Pipeline(db, registry)

    targets = campaigns if all_campaigns else [c for c in campaigns if c.db_name == campaign_name]
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
            click.echo(f"\n  ⚠ U9itus API not configured. Set U9ITUS_BASE_URL and U9ITUS_AGENCY_TOKEN in .env")


@cli.command()
@click.option("--campaign", "campaign_name", help="Specific campaign")
@click.option("--all", "all_campaigns", is_flag=True, help="Pull events for all campaigns")
@click.option("--dry-run", is_flag=True, help="Show events without updating the pipeline")
@click.pass_context
def pull_events(ctx, campaign_name, all_campaigns, dry_run):
    """Pull portal events from u9itus and auto-advance pipeline stages."""
    if not campaign_name and not all_campaigns:
        click.echo("Error: specify --campaign <name> or --all")
        sys.exit(1)

    registry, db, campaigns = _setup(db_path=ctx.obj["db_path"])
    pipeline = Pipeline(db, registry)

    targets = campaigns if all_campaigns else [c for c in campaigns if c.db_name == campaign_name]
    for campaign in targets:
        click.echo(f"\n{'='*60}")
        click.echo(f"Pulling events: {campaign.name}")
        click.echo(f"{'='*60}")
        stats = pipeline.pull_product_events(campaign, dry_run=dry_run)
        click.echo(f"  Events pulled:    {stats['events_pulled']}")
        click.echo(f"  Stage changes:    {stats['stage_changes']}")
        click.echo(f"  Already processed:{stats['already_processed']}")
        if stats.get("api_not_configured"):
            click.echo(f"\n  ⚠ U9itus API not configured. Set U9ITUS_BASE_URL and U9ITUS_AGENCY_TOKEN in .env")


@cli.command()
@click.option("--campaign", "campaign_name", help="Specific campaign")
@click.option("--all", "all_campaigns", is_flag=True, help="Check all campaigns")
@click.pass_context
def stale(ctx, campaign_name, all_campaigns):
    """Move stale prospects to nurture stage."""
    if not campaign_name and not all_campaigns:
        click.echo("Error: specify --campaign <name> or --all")
        sys.exit(1)

    registry, db, campaigns = _setup(db_path=ctx.obj["db_path"])
    pipeline = Pipeline(db, registry)

    targets = campaigns if all_campaigns else [c for c in campaigns if c.db_name == campaign_name]
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

    registry, db, campaigns = _setup(db_path=ctx.obj["db_path"])
    pipeline = Pipeline(db, registry)

    targets = campaigns if all_campaigns else [c for c in campaigns if c.db_name == campaign_name]
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

    registry, db, campaigns = _setup(db_path=ctx.obj["db_path"])
    pipeline = Pipeline(db, registry)

    targets = campaigns if all_campaigns else [c for c in campaigns if c.db_name == campaign_name]
    for campaign in targets:
        click.echo(f"\n{'='*60}")
        click.echo(f"Digest: {campaign.name}")
        click.echo(f"{'='*60}")
        stats = pipeline.weekly_digest(campaign)
        click.echo(f"  Total prospects:  {stats['total_prospects']}")
        click.echo(f"  Emails sent:      {stats['total_emails_sent']}")
        click.echo(f"  Open rate:        {stats['open_rate']}")
        click.echo(f"  Reply rate:       {stats['reply_rate']}")
        click.echo(f"  Stage breakdown:")
        for stage, count in sorted(stats["stage_counts"].items()):
            click.echo(f"    {stage:20s} {count}")


@cli.command()
@click.pass_context
def campaigns(ctx):
    """List all discovered campaigns."""
    registry, db, all_campaigns = _setup(db_path=ctx.obj["db_path"])
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
    registry, db, all_campaigns = _setup(db_path=ctx.obj["db_path"])
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


# ── Users ──────────────────────────────────────────────────────────────
# Day-to-day user and role management happens in the web UI (/admin/users).
# These commands cover bootstrap and recovery, so they run as "cli" in the
# audit log and need shell access to the server instead of a login.


def _access_db(ctx) -> Database:
    db = Database(ctx.obj["db_path"])
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


if __name__ == "__main__":
    cli()