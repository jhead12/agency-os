# agency-os CLI Reference

Complete command reference for agency-os. Run from the project root.

## Setup

```bash
# Install dependencies
pip install -r requirements.txt

# Load environment variables (DATABASE_URL must point at PostgreSQL)
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

### Make yourself a Super Admin (bootstrap, once)

Only a Super Admin can create Owners or grant/remove the Owner and Super Admin roles,
in the dashboard, the console, or the remote CLI. Run this once on the server to
make the first one:

```bash
python3 agency_os.py users grant-super-admin --email joshua@u9itus.com
```

The `users ...` commands above talk to the database directly (`DATABASE_URL`), so
they need shell access to the server and can do anything. Keep them for bootstrap
and recovery; use the console below day to day.

---

## Command Console (browser and remote CLI)

Users whose role includes `cli.use` get **Console** in the Account menu (`/console`):
a terminal in the browser. It is **not a server shell**. Each line runs an agency-os
command as the signed-in user, with that user's permissions, so it lists and runs
only what their role allows. Changes ask `Run this? [y/N]` first and are audited.

The same commands work from your own terminal against a deployed server:

```bash
# Once: create a CLI key on your Account page (Command console → Create CLI key), then
python3 agency_os.py connect --url https://your-app.up.railway.app --key aos_cli_...

python3 agency_os.py remote help                       # what you can run
python3 agency_os.py remote users invite --email jane@u9itus.com --name "Jane" --role Caller
python3 agency_os.py remote --yes users list           # --yes skips the confirmation
python3 agency_os.py remote                            # interactive prompt
```

The key is saved to `~/.config/agency-os/cli.json` (readable only by you);
`AGENCY_OS_URL` and `AGENCY_OS_KEY` override it. CLI keys work only on the console,
not on `/mcp` or `/api/tools`, and stop working when revoked on the Account page
or when the user is deactivated.

### Console commands

| Command | Who | What |
|---|---|---|
| `help [command]` | everyone | List your commands, or one command's flags |
| `whoami` | everyone | Who you're signed in as |
| `users list` | Owner | Team members and their roles |
| `users invite --email E [--name N] [--role R ...] [--no-send]` | Owner | Add a member and email a one-time set-password link (`--no-send` shows the link instead). Inviting with Owner or Super Admin needs a Super Admin |
| `users create-owner --email E [--name N] [--no-send]` | Super Admin | Add a new Owner (they set their own password from the link) |
| `users set-roles --email E [--role R ...]` | Owner | Replace a member's roles; Owner/Super Admin changes need a Super Admin |
| `search-prospects`, `get-prospect`, `list-calls`, `get-campaign`, `log-call`, `set-stage`, `add-prospect-note`, ... | per permission | Every tool in `core/tools.py` the user's role allows |

Commands come from the tool registry (`core/tools.py`): a new tool shows up in the
console, the remote CLI and (unless it's console-only) AI assistants automatically.
Team-administration tools are console-only; AI assistants never see them.

---

## Roles & Permissions

### Built-in Roles

| Role | Description | Permissions |
|---|---|---|
| **Super Admin** | Everything an Owner can do, plus creating Owners and granting/removing the Owner and Super Admin roles. Owners can't change a Super Admin's account. | All permissions (automatic) |
| **Owner** | Full access. Bypasses all permission checks. Only role that can manage users, roles, and audit log. | All permissions (automatic) |
| **Caller** | Works the phones: views prospects and scripts, logs calls, moves stages | `dashboard.view`, `prospects.view`, `pipeline.edit`, `calls.view`, `calls.log`, `calendar.view` |
| **Sales Rep** | Caller access plus editing prospects and reading sent email | `dashboard.view`, `prospects.view`, `prospects.export`, `prospects.edit`, `pipeline.edit`, `calls.view`, `calls.log`, `calendar.view`, `campaigns.view`, `emails.view`, `templates.view` |
| **Template Editor** | Writes and edits outreach email templates | `dashboard.view`, `campaigns.view`, `templates.view`, `templates.edit` |
| **Viewer** | Read-only access to everything except sent email bodies | `dashboard.view`, `prospects.view`, `calls.view`, `calendar.view`, `campaigns.view`, `templates.view` |

### Permission Catalog

| Permission | Description |
|---|---|
| `dashboard.view` | View the dashboard and pipeline stats |
| `prospects.view` | View the prospect list and prospect detail pages |
| `prospects.export` | Print the full (unpaginated) prospect list |
| `prospects.edit` | Edit organization and contact info |
| `pipeline.edit` | Move prospects between pipeline stages |
| `calls.view` | View the call log and call scripts |
| `calls.log` | Record calls |
| `calendar.view` | View the follow-up calendar and .ics feed |
| `campaigns.view` | View campaign configuration |
| `emails.view` | View sent emails, including full bodies |
| `templates.view` | View and preview email templates |
| `templates.edit` | Edit email templates |
| `cli.use` | Use the command console (browser and remote CLI); each command still needs its own permission |

### Notes

- **Owner** is a protected role — it bypasses all permission checks and is the only role that can manage users, roles, and the audit log.
- **Super Admin** is a protected role above Owner. Only Super Admins (or the server-side `users` commands) can grant or remove Owner or Super Admin. Create the first one with `users grant-super-admin`.
- No starter role includes `cli.use`; add it to a role on Team → Roles to give its members the console.
- New users get **no access** until an owner assigns them a role.
- Owners can create custom roles by picking from the permission catalog via the dashboard's Team → Roles page.
- Starter roles are created once on first run. Owners may edit or delete them afterwards.
- A user can hold multiple roles — their effective permissions are the union of all roles' permissions.

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

### Import an old SQLite database (one time)

```bash
# Copies every table into the empty database at $DATABASE_URL, keeping ids
python3 agency_os.py import-sqlite --from db.sqlite
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

# Direct mail (Lob)
LOB_API_KEY=
LOB_FROM_NAME=Joshua
LOB_FROM_ADDRESS_LINE1=
LOB_FROM_ADDRESS_CITY=
LOB_FROM_ADDRESS_STATE=CA
LOB_FROM_ADDRESS_ZIP=

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
DATABASE_URL=postgresql://localhost/agency_os
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