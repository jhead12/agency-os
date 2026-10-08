"""
The generic pipeline engine.

Works for any campaign config + any combination of plugins.
The core loop: sync prospects → enrich contacts → enqueue outreach →
send emails → update stages → check stale → weekly digest.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta
from typing import Optional

from core.campaign import CadenceStep, CampaignConfig, discover_campaigns
from core.db import Database
from core import compliance, lead_packages
from core.models import Prospect, SendResult
from core.protocols import DemoPortalProduct, portal_product
from core.registry import PluginRegistry


class Pipeline:
    """Campaign-agnostic outreach pipeline."""

    def __init__(self, db: Database, registry: PluginRegistry):
        self.db = db
        self.registry = registry
        # x402 royalty payments for package leads (core/lead_packages.py);
        # tests swap in fakes. None means the defaults.
        self.payer = None
        self.http = None

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
            if hasattr(source, "bind_db"):
                source.bind_db(self.db)  # sources with usage limits and a cache keep them here
            try:
                for prospect in source.discover(campaign.filters):
                    stats["discovered"] += 1
                    if dry_run:
                        print(f"    [dry-run] {prospect.name} ({prospect.city}, {prospect.state})")
                        continue
                    pid = self.db.upsert_prospect(prospect)
                    outreach_id = self.db.upsert_outreach(pid, campaign_id)
                    if prospect.metadata.get("contact"):
                        self.db.seed_contact(outreach_id, prospect.metadata["contact"])
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

        enriched = self.enrich_outreach_rows(rows, campaign.enrichers)
        return {"enriched": enriched, "checked": len(rows)}

    def enrich_outreach_rows(self, rows, enricher_keys, *, stop=None, after_each=None) -> int:
        """Run enrichers, in order, on outreach rows ({id, prospect_id}); the first hit wins.
        `stop()` ends early (e.g. a cancelled generator run); `after_each(enriched)` reports progress.
        Returns how many rows got something."""
        enriched = 0
        for row in rows:
            if stop is not None and stop():
                break
            prospect = self.db.get_prospect(row["prospect_id"])
            if not prospect:
                continue
            for enricher_key in enricher_keys:
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
            if after_each is not None:
                after_each(enriched)
        return enriched

    # ── Enqueue Outreach ───────────────────────────────────────────────

    def enqueue_outreach(self, campaign: CampaignConfig, limit: int = 50, dry_run: bool = False, test_email: str = "") -> dict:
        """Find due follow-ups, draft personalized emails, send via channel.

        If test_email is set, ALL outbound emails are redirected to that address
        instead of the prospect's real email. This lets you test the full pipeline
        (scripts, personalization, sending) without emailing real prospects.
        """
        stats = {"sent": 0, "skipped": 0, "failed": 0, "no_contact": 0, "royalty_blocked": 0, "suppressed": 0}
        campaign_id = self.db.get_campaign_id(campaign.db_name)
        if not campaign_id:
            return {**stats, "error": "campaign not found"}

        due = self.db.get_due_outreach(campaign.db_name, limit, stages=campaign.sequence_stages)
        print(f"  → {len(due)} prospects due for outreach")
        # Email channels don't send without an unsubscribe link and postal address
        email_blocked = compliance.problem()
        if email_blocked and not dry_run:
            print(f"  ! Email channels off: {email_blocked}")

        for outreach in due:
            # Need some way to reach them — if not, skip (or enrich)
            if not (outreach.contact_email or outreach.contact_phone):
                stats["no_contact"] += 1
                continue

            # In test mode, redirect all emails to the test address
            send_email = outreach.contact_email or ""
            send_phone = outreach.contact_phone or ""
            if test_email:
                send_email = test_email
                send_phone = ""  # don't send SMS in test mode

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

            # Leads bought as an x402 package: pay the royalty before the first
            # touch, and only text them if the package includes SMS consent.
            # A no-op for every other prospect.
            gate = lead_packages.gate_contact(
                self.db, campaign, campaign_id, prospect, outreach.touch_count,
                dry_run=dry_run, http=self.http, payer=self.payer,
            )
            if not gate.send:
                print(f"  ! Skipping {prospect.name}: {gate.reason}")
                stats["royalty_blocked"] += 1
                continue
            if gate.reason:
                print(f"    [dry-run] {prospect.name}: {gate.reason}")

            variables = self._build_variables(campaign, prospect, outreach)
            subject = self._render(script.get("subject", ""), variables)
            body = self._render(script.get("body", ""), variables)

            if dry_run:
                print(f"\n    [dry-run] To: {send_email or send_phone}")
                print(f"    Subject: {subject}")
                print(f"    Body: {body[:120]}...")
                stats["sent"] += 1
                continue

            # Send via the first configured channel that can reach them
            # (a channel returns "skipped" when e.g. there's no phone number)
            result = None
            suppressed = False
            sent_body = body
            for ch_key in step.channels or campaign.channels:
                if ch_key.startswith("sms") and (not gate.sms_allowed or self.db.do_not_call(prospect.id)):
                    continue
                channel = self.registry.get_channel(ch_key)
                if not channel or not channel.is_configured():
                    continue
                extra = {}
                sent_body = body
                if ch_key.startswith("email"):
                    if email_blocked:
                        continue
                    # The real contact's opt-out counts even when test mode redirects the send
                    if self.db.email_suppressed(send_email) or self.db.email_suppressed(outreach.contact_email):
                        suppressed = True
                        continue
                    sent_body = compliance.with_footer(body, send_email)
                    extra = {"unsubscribe_url": compliance.unsubscribe_url(send_email)}
                try:
                    result = channel.send(
                        recipient={
                            "email": send_email,
                            "phone": send_phone,
                            "name": outreach.contact_name or "",
                            # Mailing address, for direct mail channels. Left out
                            # in test mode, which must not mail real prospects.
                            **({} if test_email else {
                                "company": prospect.name,
                                "address": prospect.address,
                                "city": prospect.city,
                                "state": prospect.state,
                                "zip": prospect.zip,
                            }),
                        },
                        subject=subject,
                        body=sent_body,
                        metadata={
                            **extra,
                            "campaign": campaign.db_name,
                            "outreach_id": outreach.id,
                            "template_key": script.get("key", step.script),
                            "campaign_id": campaign_id,
                            "variables": variables,
                            # mail_*.yaml scripts: front/back etc. with variables filled in
                            **({"mail_template": {
                                k: self._render(v, variables) if isinstance(v, str) else v
                                for k, v in script.items()
                            }} if script.get("mail_type") else {}),
                        },
                    )
                except Exception as exc:
                    result = SendResult(status="failed", error=str(exc))
                if result.status != "skipped":
                    break

            if suppressed and (result is None or result.status == "skipped"):
                print(f"  · {outreach.contact_email} unsubscribed; not emailed")
                stats["suppressed"] += 1
                continue
            if result is None:
                print(f"  ! No configured channel for {outreach.id}")
                stats["failed"] += 1
                continue
            if result.status == "skipped":
                # No channel could reach this contact — don't advance the sequence
                stats["no_contact"] += 1
                continue

            # Log the email
            self.db.log_email(
                outreach_id=outreach.id,
                campaign_id=campaign_id,
                template_key=script.get("key", step.script),
                subject=subject,
                body=sent_body,
                result=result,
            )

            # Update outreach row — stage only moves forward, never backward
            now = datetime.now()
            stage_order = {
                "cold": 0, "contacted": 1, "engaged": 2,
                "demo_scheduled": 3, "proposal_sent": 4,
                "closed_won": 5, "closed_lost": 5, "nurture": 5,
            }
            current_stage_rank = stage_order.get(outreach.stage, 0)
            next_stage_rank = stage_order.get(step.next_stage, 0)
            # Keep the higher stage (never move backward)
            final_stage = step.next_stage if next_stage_rank > current_stage_rank else outreach.stage

            self.db.update_outreach(outreach.id, {
                "touch_count": outreach.touch_count + 1,
                "last_contacted_at": now.isoformat(),
                "next_follow_up_at": (now + timedelta(days=step.delay_days)).isoformat(),
                "stage": final_stage,
                "script_variant": step.script,
            })

            if result.status == "sent":
                stats["sent"] += 1
            elif result.status == "failed":
                stats["failed"] += 1
            else:
                stats["skipped"] += 1

        return stats

    # ── Booking Sync ───────────────────────────────────────────────────

    def sync_bookings(self, campaign: CampaignConfig, days_back: int = 30, dry_run: bool = False) -> dict:
        """Pull meetings from the campaign's scheduler and update the pipeline.

        New bookings move the prospect to demo_scheduled and set the follow-up
        date to the meeting time. Cancellations move them back to engaged.
        """
        stats = {"booked": 0, "canceled": 0, "unmatched": 0, "unchanged": 0}
        scheduler = self.registry.get_scheduler(campaign.scheduler) if campaign.scheduler else None
        if not scheduler or not scheduler.is_configured():
            return {**stats, "error": "no configured scheduler"}
        campaign_id = self.db.get_campaign_id(campaign.db_name)
        if not campaign_id:
            return {**stats, "error": "campaign not found"}

        # Stages a booking should never pull a prospect back from
        later_stages = {"proposal_sent", "closed_won", "closed_lost"}

        for booking in scheduler.fetch_bookings(datetime.now() - timedelta(days=days_back)):
            outreach = None
            if booking.outreach_id:
                outreach = self.db.get_outreach(booking.outreach_id)
                if outreach and outreach.campaign_id != campaign_id:
                    outreach = None
            if outreach is None and booking.invitee_email:
                outreach = self.db.find_outreach_by_email(campaign.db_name, booking.invitee_email)
            if outreach is None:
                stats["unmatched"] += 1
                continue

            log = list(outreach.activity_log)
            event_type = "meeting_canceled" if booking.status == "canceled" else "meeting_booked"
            if any(e.get("type") == event_type and e.get("ref") == booking.external_id for e in log):
                stats["unchanged"] += 1
                continue

            when = booking.start_time.strftime("%b %d, %Y %I:%M %p") if booking.start_time else "unknown time"
            log.append({
                "at": datetime.now().isoformat(),
                "type": event_type,
                "ref": booking.external_id,
                "detail": f"{booking.event_name or 'Meeting'} with {booking.invitee_name or booking.invitee_email} — {when}",
                "join_url": booking.join_url,
            })
            updates: dict = {"activity_log": log}

            if booking.status == "canceled":
                # A prospect who claimed their demo portal is still in the
                # product, so a canceled meeting doesn't move them back (A10).
                claimed = any(e.get("type") == "portal.claimed" for e in log)
                if outreach.stage == "demo_scheduled" and not claimed:
                    updates["stage"] = "engaged"
                    updates["next_follow_up_at"] = datetime.now().isoformat()
                stats["canceled"] += 1
            else:
                if outreach.stage not in later_stages:
                    updates["stage"] = "demo_scheduled"
                if booking.start_time:
                    updates["next_follow_up_at"] = booking.start_time.isoformat()
                stats["booked"] += 1

            print(f"    → [{scheduler.key}] {event_type}: {booking.invitee_email} ({when})")
            if not dry_run:
                self.db.update_outreach(outreach.id, updates)

        return stats

    # ── Stale Check ────────────────────────────────────────────────────

    def check_stale(self, campaign: CampaignConfig) -> dict:
        """Move prospects with no contact in N days to nurture."""
        count = self.db.move_stale_to_nurture(campaign.db_name, campaign.stale_threshold_days)
        return {"moved_to_nurture": count, "threshold_days": campaign.stale_threshold_days}

    # ── Weekly Digest ──────────────────────────────────────────────────

    def weekly_digest(self, campaign: CampaignConfig) -> dict:
        """Gather pipeline stats for a campaign, plus lead-package spend if it uses packages."""
        stats = self.db.get_pipeline_stats(campaign.db_name)
        campaign_id = self.db.get_campaign_id(campaign.db_name) if campaign.lead_packages else None
        if campaign_id:
            stats["spend"] = self.db.campaign_spend(campaign_id)
        return stats

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

        # Scheduler plugin provides a personalized booking link
        scheduler = self.registry.get_scheduler(campaign.scheduler) if campaign.scheduler else None
        if scheduler and scheduler.is_configured():
            booking_link = scheduler.booking_link(prospect, outreach)
            if booking_link:
                variables["booking_link"] = booking_link

        # Product plugin provides value prop + demo link
        product = self.registry.get_product(campaign.product)
        if product:
            variables["value_prop"] = product.describe_value(prospect)
            # Pass the provisioned portal link; without it the product falls
            # back to the generic /compare page.
            demo_link = product.generate_demo_link(prospect, demo_link=getattr(outreach, "demo_link", None))
            if demo_link:
                variables["demo_link"] = demo_link

        return variables

    def _render(self, template: str, variables: dict) -> str:
        """Simple {{variable}} substitution."""
        def replace(match):
            key = match.group(1).strip()
            return str(variables.get(key, match.group(0)))

        return re.sub(r"\{\{(\w+)\}\}", replace, template)

    # ── Demo Portal Provisioning (A3) ─────────────────────────────────

    def provision_demos(self, campaign: CampaignConfig, limit: int = 50, dry_run: bool = False) -> dict:
        """Provision personal demo portals for prospects with contact emails.

        Only provisions prospects at cold/contacted stage with a contact email
        and no existing demo_link. Idempotent: re-running creates no duplicates.
        """
        stats = {"provisioned": 0, "skipped": 0, "failed": 0, "no_contact": 0}
        campaign_id = self.db.get_campaign_id(campaign.db_name)
        if not campaign_id:
            return {**stats, "error": "campaign not found"}

        product = portal_product(self.registry.get_product(campaign.product))
        if not product:
            return {**stats, "error": "product does not support demo provisioning"}

        if not product.is_configured():
            return {**stats, "api_not_configured": True, "setup_hint": product.setup_hint()}

        # Find prospects that need provisioning: have email, no demo_link, in cold/contacted
        rows = self.db.conn.execute(
            """SELECT o.id, o.prospect_id, o.contact_email, o.demo_link, o.stage
               FROM outreach o
               WHERE o.campaign_id = ?
                 AND o.contact_email IS NOT NULL AND o.contact_email != ''
                 AND (o.demo_link IS NULL OR o.demo_link = '')
                 AND o.stage IN ('cold', 'contacted')
               LIMIT ?""",
            (campaign_id, limit),
        ).fetchall()

        print(f"  → {len(rows)} prospects need demo portal provisioning")

        for row in rows:
            prospect = self.db.get_prospect(row["prospect_id"])
            if not prospect:
                stats["failed"] += 1
                continue

            if dry_run:
                print(f"    [dry-run] {prospect.name} ({prospect.state}) → would provision")
                stats["provisioned"] += 1
                continue

            try:
                result = product.provision_demo(
                    prospect,
                    contact_email=row["contact_email"],
                )

                if result.get("error"):
                    print(f"    ! Failed: {prospect.name} — {result.get('detail', 'unknown')}")
                    stats["failed"] += 1
                    continue

                demo_url = result.get("demo_url", "")
                if demo_url:
                    self._store_portal(product, prospect, result, [row["id"]])
                    print(f"    ✓ {prospect.name} → {demo_url[:60]}...")
                    stats["provisioned"] += 1
                else:
                    stats["skipped"] += 1

            except Exception as exc:
                print(f"    ! Error provisioning {prospect.name}: {exc}")
                stats["failed"] += 1

        return stats

    def provision_prospect(self, campaign: CampaignConfig, prospect_id: int, refresh: bool = False) -> dict:
        """Provision (or, with refresh, renew) one prospect's demo portal — the
        dashboard's "Create demo page" button. Unlike provision_demos() it ignores
        the stage filter: a person chose this prospect on purpose.

        Returns the API result, or {"error": True, "detail": ...}.
        """
        product = portal_product(self.registry.get_product(campaign.product))
        if not product:
            return {"error": True, "detail": "This campaign's product can't create demo pages."}
        if not product.is_configured():
            return {"error": True, "detail": f"The API isn't configured. {product.setup_hint()}."}

        prospect = self.db.get_prospect(prospect_id)
        if not prospect:
            return {"error": True, "detail": "Prospect not found."}

        rows = self.db.conn.execute(
            "SELECT id, contact_email FROM outreach WHERE prospect_id = ?", (prospect_id,),
        ).fetchall()
        contact_email = next((r["contact_email"] for r in rows if r["contact_email"]), "")

        result = product.provision_demo(prospect, contact_email=contact_email, refresh=refresh)
        if result.get("error"):
            return result
        if result.get("demo_url"):
            self._store_portal(product, prospect, result, [r["id"] for r in rows])
        else:
            self._merge_portal_metadata(product, prospect_id, {"status": result.get("status")})
        return result

    def refresh_portal_status(self, campaign: CampaignConfig, prospect_id: int) -> dict:
        """Fetch a prospect's portal status and 30-day views from the product and keep them on the prospect."""
        product = portal_product(self.registry.get_product(campaign.product))
        prospect = self.db.get_prospect(prospect_id)
        if not product or not prospect:
            return {"error": True, "detail": "Status isn't available for this prospect."}

        result = product.get_portal_status(prospect)
        if result.get("error"):
            return result

        traffic = result.get("traffic") or {}
        self._merge_portal_metadata(product, prospect_id, {
            "status": result.get("status"),
            "expires_at": result.get("expires_at"),
            "views_30d": traffic.get("views_30d"),
            "last_viewed_on": traffic.get("last_viewed_on"),
            "checked_at": datetime.now().isoformat(timespec="seconds"),
        })
        return result

    def _store_portal(self, product: DemoPortalProduct, prospect: Prospect, result: dict,
                      outreach_ids: list[int]) -> None:
        """Save a provisioned portal: the demo link on the outreach rows, details on the prospect."""
        for outreach_id in outreach_ids:
            self.db.update_outreach(outreach_id, {"demo_link": result.get("demo_url")})
        self._merge_portal_metadata(product, prospect.id, {
            "slug": result.get("slug"),
            "claim_url": result.get("claim_url"),
            "status": result.get("status"),
            "expires_at": result.get("expires_at"),
        })

    def _merge_portal_metadata(self, product: DemoPortalProduct, prospect_id: int, values: dict) -> None:
        """Update prospect.metadata[<product's portal_namespace>], keeping keys not in `values`."""
        row = self.db.conn.execute("SELECT metadata FROM prospects WHERE id = ?", (prospect_id,)).fetchone()
        if not row:
            return
        metadata = json.loads(row["metadata"] or "{}")
        ns = product.portal_namespace
        metadata[ns] = {**(metadata.get(ns) or {}), **values}
        self.db.conn.execute(
            "UPDATE prospects SET metadata = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (json.dumps(metadata), prospect_id),
        )

    # ── Pull Product Events (A4) ──────────────────────────────────────

    # Stage mapping: event type → target stage (only moves forward).
    # portal.published is listed so it is processed, but never changes the stage.
    EVENT_STAGE_MAP = {
        "portal.viewed": "engaged",
        "portal.claimed": "demo_scheduled",
        "portal.published": "demo_scheduled",
    }

    # Stage order for "never move backward" guard
    STAGE_ORDER = {
        "cold": 0, "contacted": 1, "engaged": 2,
        "demo_scheduled": 3, "proposal_sent": 4,
        "closed_won": 5, "closed_lost": 5, "nurture": 5,
    }

    def pull_product_events(self, campaign: CampaignConfig, dry_run: bool = False) -> dict:
        """Pull the product's portal events and auto-advance pipeline stages.

        Idempotent: events already processed (in product_events table) are skipped.
        """
        stats = {"events_pulled": 0, "stage_changes": 0, "already_processed": 0}
        campaign_id = self.db.get_campaign_id(campaign.db_name)
        if not campaign_id:
            return {**stats, "error": "campaign not found"}

        product = portal_product(self.registry.get_product(campaign.product))
        if not product:
            return {**stats, "error": "product does not support event pulling"}

        if not product.is_configured():
            return {**stats, "api_not_configured": True, "setup_hint": product.setup_hint()}

        # Get last cursor from sync_cursors
        cursor = self._get_cursor(campaign.product)

        # Pull events (may need multiple pages)
        all_events = []
        while True:
            result = product.pull_events(after=cursor, limit=100)

            if result.get("error"):
                print(f"  ! API error: {result.get('detail')}")
                return {**stats, "error": result.get("detail")}

            events = result.get("events", [])
            next_cursor = result.get("next_cursor", cursor)

            if not events:
                break

            all_events.extend(events)
            cursor = next_cursor

            if len(events) < 100:
                break  # last page

        print(f"  → {len(all_events)} events pulled since cursor {self._get_cursor(campaign.product)}")

        for event in all_events:
            event_id = event.get("id")
            event_type = event.get("type")
            external_ref = event.get("external_ref", "")

            # Extract prospect ID from external_ref (format: agency-os:prospect:1234)
            prospect_id = None
            if external_ref.startswith("agency-os:prospect:"):
                try:
                    prospect_id = int(external_ref.split(":")[-1])
                except ValueError:
                    pass

            if not prospect_id:
                stats["already_processed"] += 1
                continue

            # Check if we already processed this event (idempotent)
            if self._event_already_processed(campaign.product, event_id):
                stats["already_processed"] += 1
                continue

            if dry_run:
                print(f"    [dry-run] {event_type} for prospect {prospect_id}")
                stats["events_pulled"] += 1
                continue

            # Record the event
            self._record_event(campaign.product, event_id, event)

            # Keep the portal's status on the prospect current for the dashboard.
            status = {"portal.claimed": "claimed", "portal.published": "published",
                      "portal.expired": "expired"}.get(event_type)
            if status:
                self._merge_portal_metadata(product, prospect_id, {"status": status})
            if event_type == "portal.expired":
                # The link no longer works; clearing it lets provisioning issue a new one.
                self.db.conn.execute(
                    "UPDATE outreach SET demo_link = NULL WHERE prospect_id = ?", (prospect_id,),
                )

            # A demo portal belongs to the prospect, not to one campaign, and the
            # event cursor is shared by every campaign using this product, so the
            # event applies to all of the prospect's outreach rows.
            outreach_rows = self.db.conn.execute(
                "SELECT id, stage, activity_log FROM outreach WHERE prospect_id = ?",
                (prospect_id,),
            ).fetchall()
            for outreach_row in outreach_rows:
                if self._apply_portal_event(product, outreach_row, event):
                    print(f"    ✓ Prospect {prospect_id}: {outreach_row['stage']} → "
                          f"{self.EVENT_STAGE_MAP[event_type]} ({event_type})")
                    stats["stage_changes"] += 1

            stats["events_pulled"] += 1

        # Save the cursor
        if not dry_run and all_events:
            self._save_cursor(campaign.product, cursor)

        return stats

    def _apply_portal_event(self, product: DemoPortalProduct, outreach_row, event: dict) -> bool:
        """Log one portal event on one outreach row and move its stage forward.

        Viewed and claimed events are always logged (A10 relies on the
        portal.claimed entry even when the stage doesn't change); published adds
        the ready_to_close flag and never changes the stage. Returns True when
        the stage changed.
        """
        event_type = event.get("type")
        target_stage = self.EVENT_STAGE_MAP.get(event_type)
        if not target_stage:
            return False

        event_ref = f"{product.portal_namespace}:{event.get('id')}"
        activity = json.loads(outreach_row["activity_log"] or "[]")
        if any(a.get("ref") == event_ref for a in activity):
            return False

        current_stage = outreach_row["stage"]
        # nurture ranks with the closed stages for the email sequence, but a
        # parked prospect who opens or claims their demo is active again.
        current_rank = 1 if current_stage == "nurture" else self.STAGE_ORDER.get(current_stage, 0)
        moves = event_type != "portal.published" and self.STAGE_ORDER.get(target_stage, 0) > current_rank

        if event_type == "portal.published":
            entry = {"type": "portal_published", "ref": event_ref, "flag": "ready_to_close"}
        else:
            entry = {"type": event_type, "ref": event_ref, "stage": target_stage if moves else current_stage}
        activity.append({**entry, "timestamp": event.get("occurred_at", "")})

        updates = {"activity_log": json.dumps(activity)}
        if moves:
            updates["stage"] = target_stage
        self.db.update_outreach(outreach_row["id"], updates)
        return moves

    def _get_cursor(self, product_key: str) -> int:
        """Get the last event cursor for a product."""
        row = self.db.conn.execute(
            "SELECT cursor_value FROM sync_cursors WHERE product_key = ?",
            (product_key,),
        ).fetchone()
        return row["cursor_value"] if row else 0

    def _save_cursor(self, product_key: str, cursor: int) -> None:
        """Save the event cursor for a product."""
        c = self.db.conn
        c.execute(
            """INSERT INTO sync_cursors (product_key, cursor_value, updated_at)
               VALUES (?, ?, CURRENT_TIMESTAMP)
               ON CONFLICT(product_key) DO UPDATE SET
                 cursor_value = excluded.cursor_value,
                 updated_at = CURRENT_TIMESTAMP""",
            (product_key, cursor),
        )

    def _event_already_processed(self, product_key: str, event_id: int) -> bool:
        """Check if an event has already been recorded in product_events."""
        row = self.db.conn.execute(
            "SELECT 1 FROM product_events WHERE product_key = ? AND event_id = ?",
            (product_key, event_id),
        ).fetchone()
        return row is not None

    def _record_event(self, product_key: str, event_id: int, event: dict) -> None:
        """Record a raw event in product_events."""
        c = self.db.conn
        c.execute(
            """INSERT INTO product_events
               (product_key, event_id, event_type, external_ref, data, processed_at)
               VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
               ON CONFLICT DO NOTHING""",
            (
                product_key,
                event_id,
                event.get("type", ""),
                event.get("external_ref", ""),
                json.dumps(event.get("data", {})),
            ),
        )
