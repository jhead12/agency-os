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

There are two ways to manage users:

- **Server-side commands** (`python3 agency_os.py users ...`, below) connect straight
  to the database at `DATABASE_URL`. They need shell access to the server (on Railway,
  `railway ssh`), skip every permission check, and are logged as `cli` in the audit
  log. Use them to bootstrap and to recover.
- **The command console** (`/console` in the browser, or `agency_os.py remote` from
  your own terminal) runs as a signed-in user with that user's permissions. Use it
  day to day; see [Command Console](#command-console-browser-and-remote-cli).

### First-time setup

```bash
# 1. Create your account as an Owner
python3 agency_os.py users create-owner --email joshua@u9itus.com --name "Joshua"

# 2. Make yourself a Super Admin, so you can create Owners from the dashboard and console
python3 agency_os.py users grant-super-admin --email joshua@u9itus.com
```

### Create an owner (bootstrap)

```bash
python3 agency_os.py users create-owner --email joshua@u9itus.com --name "Joshua" --password "your-password"
```

Options:
- `--email` (required) — user's email
- `--name` — display name (defaults to email's local part)
- `--password` — password (prompted if omitted; passing it on the command line leaves it in your shell history)

### List all users

```bash
python3 agency_os.py users list
```


### Invite a new team member (sends welcome email)

```bash
# Invite a caller
python3 agency_os.py users invite --email jane@u9itus.com --name "Jane" --role "Caller"

# Invite with multiple roles
python3 agency_os.py users invite --email bob@u9itus.com --name "Bob" --role "Caller" --role "Template Editor"

# Invite an owner (server-side commands may grant Owner; in the console only a Super Admin can)
python3 agency_os.py users invite --email bob@u9itus.com --name "Bob" --role "Owner"

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

Re-inviting an existing user sends a fresh link (the old one stops working) and
leaves their roles alone, so it doubles as a password reset.

### Grant owner role (recovery)

```bash
python3 agency_os.py users grant-owner --email jane@u9itus.com
```

### Reset a user's password (recovery)

```bash
python3 agency_os.py users set-password --email jane@u9itus.com --password "new-password"
```

### Grant Super Admin (bootstrap or recovery)

Only a Super Admin can create Owners or grant/remove the Owner and Super Admin roles
in the dashboard, the console, or the remote CLI. Owners can't change a Super Admin's
account. Make the first one from the server:

```bash
python3 agency_os.py users grant-super-admin --email joshua@u9itus.com
```

This also reactivates the user if they were deactivated.

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

### Interactive session

```
$ python3 agency_os.py remote
agency-os at https://your-app.up.railway.app. Type help, or exit to quit.
agency-os $ users invite --email jane@u9itus.com --name "Jane" --role Caller
users invite: Add a team member and send them a one-time set-password link. ...
Run this? [y/N]: y
email: jane@u9itus.com
roles: Caller
sent: Welcome email sent (link expires Oct 13, 2026)
agency-os $ exit
```

Without SMTP configured (or with `--no-send`), the result shows the one-time link
for you to share instead of emailing it.

### Console commands

| Command | Who | What |
|---|---|---|
| `help [command]` | everyone | List your commands, or one command's flags |
| `whoami` | everyone | Who you're signed in as |
| `users list` | Owner | Team members and their roles |
| `users invite --email E [--name N] [--role R ...] [--no-send]` | Owner | Add a member and email a one-time set-password link (`--no-send` shows the link instead). Inviting with Owner or Super Admin needs a Super Admin |
| `users create-owner --email E [--name N] [--no-send]` | Super Admin | Add a new Owner (they set their own password from the link) |
| `users set-roles --email E [--role R ...]` | Owner | Replace a member's roles; Owner/Super Admin changes need a Super Admin |
| `campaigns members --campaign C` | Owner | Who works a campaign (members only, or everyone) |
| `campaigns assign --campaign C --user E` / `--role R` | Owner | Limit a campaign to its members: a person, or everyone with a role |
| `campaigns unassign --campaign C --user E` / `--role R` | Owner | Remove a member; with none left, everyone whose role allows it sees it again |
| `campaigns owners --campaign C` | Owner | Which Owners run a campaign |
| `campaigns add-owner --campaign C --user E` | Super Admin | Give a campaign to specific Owners; other Owners stop seeing it |
| `campaigns remove-owner --campaign C --user E` | Super Admin | Unassign; with none left, every Owner sees it again |
| `workflows list` | everyone | Tutorials and your own workflows |
| `workflows play --name N` | everyone | Play one in the browser (the remote CLI tells you to open the browser) |
| `workflows export` | everyone | Print a backup of your workflows: `agency_os.py remote workflows export > backup.json` |
| `search-prospects`, `get-prospect`, `list-calls`, `get-campaign`, `log-call`, `set-stage`, `add-prospect-note`, ... | per permission | Every tool in `core/tools.py` the user's role allows |

Commands come from the tool registry (`core/tools.py`): a new tool shows up in the
console, the remote CLI and (unless it's console-only) AI assistants automatically.
Team-administration tools are console-only; AI assistants never see them.

---

## Tutorials & Workflows

**Account → Tutorials & workflows** (`/workflows`). Press Play and a player acts the
workflow out on your own screen: a cursor moves to each element, fields are typed into,
pages change, console commands run in the console window, and a caption explains each
step. Pause, Next and Stop sit in a bar at the bottom; it keeps its place across pages.

- **Tutorials** are built in (`workflows/tutorials/*.yaml`) and only shown to roles
  that can do what they teach.
- **Your workflows** are saved per user. Write one on the page (YAML or JSON), press
  *Try it* to watch it before saving.
- **Backup:** *Export all* downloads one JSON file; *Import* reads it back (here or on
  another agency-os). Same-named workflows are replaced, and a file with any problem
  imports nothing.

A workflow is data, never code, so it's safe to import one from someone else: it can
only do what the person playing it could do by hand. Pages are limited to this app,
and anything that changes data asks first (a click that submits a form shows
*Do it / Skip*; a console command asks `Run this? [y/N]`).

```yaml
name: My morning check
description: Cold leads first.
steps:
  - goto: /prospects?stage=cold              # open a page of this app
    say: These are today's cold leads.        # a caption (any step can have one)
  - fill: {target: 'input[name="q"]', value: food}   # type into a field
  - click: '#filter-form button[type="submit"]'      # click (a saving click asks first)
  - wait: {for: table.data-table}            # or wait: 1000 (milliseconds)
  - highlight: table.data-table              # point at something
  - run: search-prospects --stage cold --limit 5     # run a console command
  - pause: Call the first one, then log it.  # wait for Next
```

---

## Roles & Permissions

### Built-in Roles

| Role | Description | Permissions |
|---|---|---|
| **Super Admin** | Everything an Owner can do, plus creating Owners, granting/removing the Owner and Super Admin roles, and assigning campaigns to specific Owners. Owners can't change a Super Admin's account. Always sees every campaign. | All permissions (automatic) |
| **Owner** | Full access. Bypasses all permission checks; manages users, roles, and the audit log. Can't grant Owner or Super Admin. | All permissions (automatic) |
| **Caller** | Works the phones: views prospects and scripts, logs calls, moves stages | `dashboard.view`, `prospects.view`, `pipeline.edit`, `calls.view`, `calls.log`, `calendar.view`, `royalties.view_own` |
| **Sales Rep** | Caller access plus editing prospects and reading sent email | `dashboard.view`, `prospects.view`, `prospects.export`, `prospects.edit`, `pipeline.edit`, `calls.view`, `calls.log`, `calendar.view`, `campaigns.view`, `emails.view`, `templates.view`, `portals.manage`, `packages.view`, `spend.view`, `agents.use`, `ai.connect`, `royalties.view_own` |
| **Recruiter** | Works recruiting campaigns (e.g. attorneys): calls, emails, and buys lead lists into them | `dashboard.view`, `prospects.view`, `prospects.export`, `prospects.edit`, `pipeline.edit`, `calls.view`, `calls.log`, `calendar.view`, `campaigns.view`, `emails.view`, `templates.view`, `packages.view`, `packages.buy`, `spend.view`, `royalties.view_own`, `recruiting.view` |
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
| `portals.manage` | Create, renew and check prospects' demo pages |
| `packages.view` | Browse x402 lead packages from approved providers |
| `packages.buy` | Unlock lead packages into a campaign (spends USDC, within your allowance) |
| `spend.view` | View lead-package spending and payment receipts |
| `agents.use` | Run the built-in AI agents (drafts only; nothing is sent) |
| `ai.connect` | Connect your own AI assistant to agency-os (WebMCP) |
| `packages.sell` | Publish our own lists as lead packages, and handle buyers' claims and refunds |
| `royalties.view_own` | See your own data royalties and set where they're paid |
| `recruiting.view` | See recruiting campaigns and their leads (e.g. attorneys), and buy lists into them |
| `cli.use` | Use the command console (in the browser and the remote CLI); commands still need their own permissions |

### Notes

- **Owner** is a protected role — it bypasses all permission checks and manages users, roles, and the audit log (but can't grant or remove Owner or Super Admin).
- **Super Admin** is a protected role above Owner. Only Super Admins (or the server-side `users` commands) can grant or remove Owner or Super Admin. Create the first one with `users grant-super-admin`.
- **Campaign members:** a campaign with no members is seen by everyone whose role allows it. Add people or roles under **Administration → Campaign Settings → (campaign) → Who works this campaign** (or `campaigns assign` in the console) and only they see it and its leads, everywhere (lists, detail pages, call log, stats, AI tools). Owners and Super Admins always see every campaign, and a campaign's `requires_permission` still applies to members.
- **Campaign Owners:** a Super Admin can assign specific Owners to a campaign (**Campaign Settings → (campaign) → Owners of this campaign**, or `campaigns add-owner`). Then only those Owners and Super Admins see and manage it; other Owners don't see it anywhere (prospects, settings, audit log), and lead packages they publish never include its leads. With no Owners assigned, every Owner sees it.
- No starter role includes `cli.use`; add it to a role on Team → Roles to give its members the console.
- New users get **no access** until an owner assigns them a role.
- Owners can create custom roles by picking from the permission catalog via the dashboard's Team → Roles page.
- Starter roles are created once on first run. Owners may edit or delete them afterwards.
- A user can hold multiple roles — their effective permissions are the union of all roles' permissions.

---

## Lead Packages & Spending (x402)

Off unless `AGENCY_OS_X402=on`; see the README's *Lead packages* section for setup.

```bash
# Packages offered by the providers in AGENCY_OS_LEAD_PROVIDERS
python3 agency_os.py packages list

# Pay for a package and import its leads (the paying user's allowance applies)
python3 agency_os.py packages unlock --campaign <campaign> --provider https://leads.example \
    --package p1 --email joshua@u9itus.com --dry-run      # show cost and budgets; don't pay
python3 agency_os.py packages unlock --campaign <campaign> --provider https://leads.example \
    --package p1 --email joshua@u9itus.com --yes          # pay without the prompt

# Re-check a campaign's package leads against the 90% guarantee (--ai adds the AI review)
python3 agency_os.py packages verify --campaign <campaign> [--ai]

# File a guarantee claim for failed leads (id from packages verify)
python3 agency_os.py packages claim --id 3 --email joshua@u9itus.com [--yes]

# Spending per campaign
python3 agency_os.py spend [--campaign <campaign>]

# Monthly allowance for a user (0 = can't spend)
python3 agency_os.py spend allowance --email jane@u9itus.com --usd 100

# Payments with no settlement read back: check each onchain, then resolve it
python3 agency_os.py spend pending
python3 agency_os.py spend resolve --id 12 --status settled --tx 0x...
python3 agency_os.py spend resolve --id 12 --status failed     # frees the budget to pay again
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

### Send a test email

Sends one email using a campaign's real scripts to `--to` (never to the prospect).

```bash
python3 agency_os.py test-send --campaign <campaign> --to you@example.com
python3 agency_os.py test-send --campaign <campaign> --to you@example.com \
    --script 01_followup_impact --prospect-id 1      # personalize with a real prospect
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

### Backfill IRS subsection (one time)

Fills `irs_subsection` from the IRS BMF for prospects missing it, so demo portals
get the right org type.

```bash
python3 agency_os.py backfill-irs-subsection --all --dry-run
python3 agency_os.py backfill-irs-subsection --campaign <campaign>
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
- `/lead-packages` — Browse and unlock x402 lead packages
- `/account` — Your roles, password, AI features, MCP keys and CLI keys
- `/console` — Command console (needs `cli.use`)
- `/workflows` — Tutorials, your workflows, backup (export/import)
- `/admin/users`, `/admin/roles`, `/admin/audit` — Team administration (Owners)

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
AGENCY_OS_BASE_URL=          # public URL, used in invite links (Railway's domain if unset)
DATABASE_URL=postgresql://localhost/agency_os

# Remote CLI (override ~/.config/agency-os/cli.json written by `connect`)
AGENCY_OS_URL=
AGENCY_OS_KEY=
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