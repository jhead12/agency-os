# agency-os CLI Reference

Complete command reference for agency-os. Run from the project root.

## Setup

```bash
# Install dependencies
pip install -r requirements.txt

# Load environment variables
source .env

# Start the web dashboard
python3 -m web.app
```

---

## User Management

### Create the first owner (bootstrap)

```bash
python3 agency_os.py users create-owner --email joshua@u9itus.com --name "Joshua" --password "your-password"
```

Options:
- `--email` (required) — user's email
- `--name` — display name (defaults to email's local part)
- `--password` — password (prompted if omitted)

### List all users

```bash
python3 agency_os.py users list
```

### Invite a new team member (sends welcome email)

```bash
# Invite a caller
python3 agency_os.py users invite --email jane@u9itus.com --name "Jane" --role "Caller"

# Invite with multiple roles
python3 agency_os.py users invite --email bob@u9itus.com --name "Bob" --role "Caller" --role "Owner"

# Preview email without sending (prints to console)
python3 agency_os.py users invite --email jane@u9itus.com --name "Jane" --role "Caller" --no-send

# Custom dashboard URL (if deployed)
python3 agency_os.py users invite --email jane@u9itus.com --name "Jane" --base-url https://your-app.up.railway.app
```

Options:
- `--email` (required) — invitee's email
- `--name` — display name (new users)
- `--role` — role for new user (repeat for multiple roles)
- `--base-url` — dashboard URL for the set-password link
- `--no-send` — print the email instead of sending it

### Grant owner role (recovery)

```bash
python3 agency_os.py users grant-owner --email jane@u9itus.com
```

### Reset a user's password (recovery)

```bash
python3 agency_os.py users set-password --email jane@u9itus.com --password "new-password"
```

---

## Prospect Management

### Sync prospects from all sources

```bash
# Sync one campaign
python3 agency_os.py sync --campaign voter-guide--cbo-outreach-los-angeles

# Sync all campaigns
python3 agency_os.py sync --all

# Dry run (don't write to DB)
python3 agency_os.py sync --all --dry-run
```

### Enrich contact info

Uses the local scraper (free, no API key) to find websites, phone numbers, and emails.

```bash
# Enrich 20 prospects
python3 agency_os.py enrich --campaign voter-guide--cbo-outreach-los-angeles --limit 20

# Enrich all campaigns
python3 agency_os.py enrich --all --limit 50
```

---

## Email Outreach

### Send outreach emails

```bash
# Send up to 50 emails
python3 agency_os.py enqueue --campaign voter-guide--cbo-outreach-los-angeles --limit 50

# Dry run (preview without sending)
python3 agency_os.py enqueue --campaign voter-guide--cbo-outreach-los-angeles --dry-run --limit 5

# All campaigns
python3 agency_os.py enqueue --all --limit 50
```

---

## u9itus Portal Integration

### Provision demo portals

Creates a personal demo portal on u9itus for each prospect with a contact email.

```bash
# Provision 10 portals
python3 agency_os.py provision --campaign voter-guide--cbo-outreach-los-angeles --limit 10

# Dry run (no API calls)
python3 agency_os.py provision --all --dry-run
```

Requires `U9ITUS_BASE_URL` and `U9ITUS_AGENCY_TOKEN` in `.env`.

### Pull portal events

Pulls events from u9itus and auto-advances pipeline stages:
- `portal.viewed` → engaged
- `portal.claimed` → demo_scheduled
- `portal.published` → flags "ready to close"

```bash
python3 agency_os.py pull-events --campaign voter-guide--cbo-outreach-los-angeles
python3 agency_os.py pull-events --all --dry-run
```

---

## Scheduling

### Sync Calendly bookings

```bash
python3 agency_os.py bookings --campaign voter-guide--cbo-outreach-los-angeles
python3 agency_os.py bookings --all --dry-run
```

Requires `CALENDLY_API_TOKEN` in `.env`.

---

## Pipeline Maintenance

### Move stale prospects to nurture

```bash
python3 agency_os.py stale --all
python3 agency_os.py stale --campaign voter-guide--cbo-outreach-los-angeles
```

### Show pipeline digest

```bash
python3 agency_os.py digest --campaign voter-guide--cbo-outreach-los-angeles
python3 agency_os.py digest --all
```

---

## Discovery

### List all campaigns

```bash
python3 agency_os.py campaigns
```

### List all plugins

```bash
# All plugins
python3 agency_os.py plugins

# By type
python3 agency_os.py plugins --type prospect_sources
python3 agency_os.py plugins --type products
python3 agency_os.py plugins --type channels
python3 agency_os.py plugins --type enrichers
```

---

## Cron Schedule

Recommended schedule for Hermes or crontab:

```bash
# ── Daily ──────────────────────────────────────────────

# 08:00 — enrich contacts (local scraper)
python3 agency_os.py enrich --all --limit 20

# 08:30 — provision new demo portals
python3 agency_os.py provision --all --limit 20

# 09:00 — send outreach emails
python3 agency_os.py enqueue --all --limit 50

# 09:30 — move stale prospects to nurture
python3 agency_os.py stale --all

# ── Hourly (8am–8pm) ──────────────────────────────────

# Pull portal events and auto-advance stages
python3 agency_os.py pull-events --all

# Sync Calendly bookings
python3 agency_os.py bookings --all

# ── Weekly ─────────────────────────────────────────────

# Monday 08:00 — pipeline digest
python3 agency_os.py digest --all

# ── Monthly ────────────────────────────────────────────

# 1st of month 03:00 — refresh prospect lists
python3 agency_os.py sync --all
```

---

## Web Dashboard

```bash
# Start the dashboard
python3 -m web.app

# Open http://localhost:8000
```

Pages:
- `/` — Dashboard with pipeline stats
- `/prospects` — Sortable, searchable prospect list
- `/prospects/{id}` — Prospect detail (edit info, stage, contact, log calls, view emails)
- `/call-scripts` — Phone call scripts (personalized, printable)
- `/call-log` — Call history with stats
- `/calendar` — Visual month calendar with ICS sync
- `/campaigns` — Campaign config viewer
- `/email-templates` — Edit email templates inline with preview
- `/emails` — Email log with expandable body viewer
- `/calendar.ics` — ICS feed for calendar sync

---

## Environment Variables

Set in `.env` (copy from `.env.example`):

```bash
# Email sending
SMARTLEAD_API_KEY=
SMTP_HOST=
SMTP_PORT=587
SMTP_USER=
SMTP_PASS=
SMTP_FROM=

# Contact enrichment
APOLLO_API_KEY=
HUNTER_API_KEY=

# u9itus portal integration
U9ITUS_BASE_URL=https://www.u9itus.com
U9ITUS_AGENCY_TOKEN=

# SMS (Twilio)
TWILIO_ACCOUNT_SID=
TWILIO_AUTH_TOKEN=
TWILIO_FROM_NUMBER=

# Scheduling (Calendly)
CALENDLY_SCHEDULING_URL=
CALENDLY_API_TOKEN=

# Dashboard
AGENCY_OS_OWNER_EMAIL=
AGENCY_OS_OWNER_PASSWORD=
AGENCY_OS_DB=db.sqlite
```

---

## Campaign Configuration

Campaign YAML lives at `campaigns/<name>/campaign.yaml`:

```yaml
name: "Voter Guide — CBO Outreach (Los Angeles)"
product: u9itus_voter_guide
prospect_sources: [irs_bmf, sos_partners, oia_grantees, miv_partners]
enrichers: [local_scraper, apollo, hunter]
channels: [email_smartlead, email_smtp, manual]
filters:
  state: CA
  county: "Los Angeles"
  ntee_codes: [R, W, P, S]
  min_revenue: 100000
sender_name: "Joshua"
sender_email: "joshua@u9itus.com"
```

Email templates: `campaigns/<name>/scripts/*.yaml`
Phone scripts: `campaigns/<name>/scripts/phone_*.yaml`

---

## Adding New Plugins

Drop a `.py` file in the right folder — it's auto-discovered:

| Type | Folder | Protocol |
|---|---|---|
| Prospect source | `plugins/prospect_sources/` | `key`, `is_configured()`, `discover(filters)` |
| Product | `plugins/products/` | `key`, `describe_value()`, `generate_demo_link()`, `pricing_tiers()` |
| Channel | `plugins/channels/` | `key`, `is_configured()`, `send()` |
| Enricher | `plugins/enrichers/` | `key`, `is_configured()`, `enrich()` |
| Scheduler | `plugins/schedulers/` | `key`, `is_configured()`, `booking_link()`, `sync_bookings()` |

No core code changes. Reference it in a campaign's YAML and it's live.