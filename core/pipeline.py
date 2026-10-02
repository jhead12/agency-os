"""
The generic pipeline engine.

Works for any campaign config + any combination of plugins.
The core loop: sync prospects → enrich contacts → enqueue outreach →
send emails → update stages → check stale → weekly digest.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Optional

from core.campaign import CampaignConfig, discover_campaigns
from core.db import Database
from core.models import Prospect, SendResult
from core.registry import PluginRegistry


class Pipeline:
    """Campaign-agnostic outreach pipeline."""

    def __init__(self, db: Database, registry: PluginRegistry):
        self.db = db
        self.registry = registry

    # ── Sync ───────────────────────────────────────────────────────────

    def sync_prospects(self, campaign: CampaignConfig, dry_run: bool = False) -> dict:
        """Run all prospect sources, upsert into DB, create outreach rows."""
        campaign_id = self.db.upsert_campaign(campaign.db_name, str(campaign.config_dir / "campaign.yaml"))
        stats = {"source": "", "discovered": 0, "upserted": 0, "errors": 0}

        for source_key in campaign.prospect_sources:
            source = self.registry.get_source(source_key)
            if not source:
                print(f"  ! Unknown prospect source: {source_key}")
                stats["errors"] += 1
                continue
            if not source.is_configured():
                print(f"  - Source not configured, skipping: {source_key}")
                continue

            print(f"  → Syncing from {source_key}...")
            stats["source"] = source_key
            try:
                for prospect in source.discover(campaign.filters):
                    stats["discovered"] += 1
                    if dry_run:
                        print(f"    [dry-run] {prospect.name} ({prospect.city}, {prospect.state})")
                        continue
                    pid = self.db.upsert_prospect(prospect)
                    self.db.upsert_outreach(pid, campaign_id)
                    stats["upserted"] += 1
            except Exception as exc:
                print(f"  ! Error in source {source_key}: {exc}")
                stats["errors"] += 1

        return stats

    # ── Enrich ─────────────────────────────────────────────────────────

    def enrich_contacts(self, campaign: CampaignConfig, limit: int = 50) -> dict:
        """Run enrichers on outreach rows missing contact info."""
        campaign_id = self.db.get_campaign_id(campaign.db_name)
        if not campaign_id:
            return {"enriched": 0, "error": "campaign not found"}

        # Find outreach rows without contact_email
        rows = self.db.conn.execute(
            """SELECT o.id, o.prospect_id FROM outreach o
               WHERE o.campaign_id = ? AND o.contact_email IS NULL
               LIMIT ?""",
            (campaign_id, limit),
        ).fetchall()

        enriched = 0
        for row in rows:
            prospect = self.db.get_prospect(row["prospect_id"])
            if not prospect:
                continue
            for enricher_key in campaign.enrichers:
                enricher = self.registry.get_enricher(enricher_key)
                if not enricher or not enricher.is_configured():
                    continue
                try:
                    result = enricher.enrich(prospect)
                    if result.contact_email or result.contact_name or result.contact_phone or result.raw.get("website"):
                        self.db.apply_enrichment(row["id"], result, prospect_id=prospect.id)
                        enriched += 1
                        break  # first hit wins
                except Exception as exc:
                    print(f"  ! Enricher {enricher_key} failed for {prospect.name}: {exc}")

        return {"enriched": enriched, "checked": len(rows)}

    # ── Enqueue Outreach ───────────────────────────────────────────────

    def enqueue_outreach(self, campaign: CampaignConfig, limit: int = 50, dry_run: bool = False) -> dict:
        """Find due follow-ups, draft personalized emails, send via channel."""
        stats = {"sent": 0, "skipped": 0, "failed": 0, "no_contact": 0}
        campaign_id = self.db.get_campaign_id(campaign.db_name)
        if not campaign_id:
            return {**stats, "error": "campaign not found"}

        due = self.db.get_due_outreach(campaign.db_name, limit)
        print(f"  → {len(due)} prospects due for outreach")

        for outreach in due:
            # Check if we have a contact email — if not, skip (or enrich)
            if not outreach.contact_email:
                stats["no_contact"] += 1
                continue

            # Find the cadence step for this touch
            step = self._get_cadence_step(campaign, outreach.touch_count)
            if step is None:
                # Sequence exhausted — move to nurture
                self.db.update_outreach(outreach.id, {"stage": "nurture"})
                stats["skipped"] += 1
                continue

            # Load and render the script
            try:
                script = campaign.load_script(step.script)
            except FileNotFoundError:
                print(f"  ! Script not found: {step.script}")
                stats["failed"] += 1
                continue

            prospect = self.db.get_prospect(outreach.prospect_id)
            if not prospect:
                stats["failed"] += 1
                continue

            variables = self._build_variables(campaign, prospect, outreach)
            subject = self._render(script.get("subject", ""), variables)
            body = self._render(script.get("body", ""), variables)

            if dry_run:
                print(f"\n    [dry-run] To: {outreach.contact_email}")
                print(f"    Subject: {subject}")
                print(f"    Body: {body[:120]}...")
                stats["sent"] += 1
                continue

            # Send via first configured channel
            result = None
            for ch_key in campaign.channels:
                channel = self.registry.get_channel(ch_key)
                if not channel or not channel.is_configured():
                    continue
                try:
                    result = channel.send(
                        recipient={
                            "email": outreach.contact_email,
                            "name": outreach.contact_name or "",
                        },
                        subject=subject,
                        body=body,
                        metadata={
                            "campaign": campaign.db_name,
                            "outreach_id": outreach.id,
                            "template_key": script.get("key", step.script),
                        },
                    )
                except Exception as exc:
                    result = SendResult(status="failed", error=str(exc))
                break  # first configured channel

            if result is None:
                print(f"  ! No configured channel for {outreach.id}")
                stats["failed"] += 1
                continue

            # Log the email
            self.db.log_email(
                outreach_id=outreach.id,
                campaign_id=campaign_id,
                template_key=script.get("key", step.script),
                subject=subject,
                body=body,
                result=result,
            )

            # Update outreach row
            now = datetime.now()
            self.db.update_outreach(outreach.id, {
                "touch_count": outreach.touch_count + 1,
                "last_contacted_at": now.isoformat(),
                "next_follow_up_at": (now + timedelta(days=step.delay_days)).isoformat(),
                "stage": step.next_stage,
                "script_variant": step.script,
            })

            if result.status == "sent":
                stats["sent"] += 1
            elif result.status == "failed":
                stats["failed"] += 1
            else:
                stats["skipped"] += 1

        return stats

    # ── Stale Check ────────────────────────────────────────────────────

    def check_stale(self, campaign: CampaignConfig) -> dict:
        """Move prospects with no contact in N days to nurture."""
        count = self.db.move_stale_to_nurture(campaign.db_name, campaign.stale_threshold_days)
        return {"moved_to_nurture": count, "threshold_days": campaign.stale_threshold_days}

    # ── Weekly Digest ──────────────────────────────────────────────────

    def weekly_digest(self, campaign: CampaignConfig) -> dict:
        """Gather pipeline stats for a campaign."""
        return self.db.get_pipeline_stats(campaign.db_name)

    # ── Helpers ────────────────────────────────────────────────────────

    def _get_cadence_step(self, campaign: CampaignConfig, touch_count: int) -> Optional[CadenceStep]:
        """Find the cadence step matching the current touch_count."""
        for step in campaign.cadence:
            if step.touch == touch_count:
                return step
        return None

    def _build_variables(self, campaign: CampaignConfig, prospect: Prospect, outreach) -> dict:
        """Build template variables for rendering."""
        variables = {
            "org_name": prospect.name,
            "contact_first": (outreach.contact_name or "").split()[0] if outreach.contact_name else "there",
            "focus_area": prospect.focus_area or "civic engagement",
            "city": prospect.city or "Los Angeles",
            "state": prospect.state or "CA",
            "your_name": campaign.sender_name,
            "your_email": campaign.sender_email,
        }

        # Product plugin provides value prop + demo link
        product = self.registry.get_product(campaign.product)
        if product:
            variables["value_prop"] = product.describe_value(prospect)
            demo_link = product.generate_demo_link(prospect)
            if demo_link:
                variables["demo_link"] = demo_link

        return variables

    def _render(self, template: str, variables: dict) -> str:
        """Simple {{variable}} substitution."""
        def replace(match):
            key = match.group(1).strip()
            return str(variables.get(key, match.group(0)))

        return re.sub(r"\{\{(\w+)\}\}", replace, template)