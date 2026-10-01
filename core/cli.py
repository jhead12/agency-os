"""
agency-os CLI.

Usage:
    agency-os sync --campaign voter-guide-cbo
    agency-os sync --all
    agency-os enqueue --campaign voter-guide-cbo --limit 50
    agency-os enrich --campaign voter-guide-cbo
    agency-os stale --all
    agency-os digest --campaign voter-guide-cbo
    agency-os campaigns
    agency-os plugins
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import click

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
@click.option("--db", default="db.sqlite", help="Path to SQLite database file")
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
@click.option("--type", "plugin_type", type=click.Choice(["prospect_sources", "products", "channels", "enrichers", "all"]), default="all")
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


if __name__ == "__main__":
    cli()