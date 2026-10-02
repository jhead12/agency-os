# agency-os

A plugin-driven, evergreen sales outreach engine. Find prospects, enrich contacts,
send sequenced emails, track pipeline progress, and generate weekly digests — for
any product or service.

## Quick start

```bash
cd agency-os
pip install -r requirements.txt
python agency_os.py campaigns          # list discovered campaigns
python agency_os.py plugins            # list discovered plugins
python agency_os.py sync --campaign voter-guide-cbo
python agency_os.py sync --campaign voter-guide-cbo --dry-run
python agency_os.py enqueue --campaign voter-guide-cbo --dry-run
python agency_os.py digest --campaign voter-guide-cbo
```

## Deploying to Railway

The web dashboard (`web/app.py`) ships as a container: Railway builds the
`Dockerfile` (per `railway.json`) and health-checks `/healthz`.

To test the image locally with Podman (or Docker):

```bash
podman build -t agency-os .
podman run --rm -p 8000:8000 \
  -e AGENCY_OS_OWNER_EMAIL=you@example.com -e AGENCY_OS_OWNER_PASSWORD=change-me-now \
  -e RAILWAY_VOLUME_MOUNT_PATH=/data -v agency-os-data:/data agency-os
```

1. Create a Railway project from this GitHub repo.
2. **Add a volume** to the service, mounted at `/data`. The app detects
   `RAILWAY_VOLUME_MOUNT_PATH` and stores `db.sqlite` and the editable
   `campaigns/` folder there (seeded from the repo on first boot), so data and
   template edits survive redeploys.
3. Set variables: `AGENCY_OS_OWNER_EMAIL` and `AGENCY_OS_OWNER_PASSWORD`
   (creates the first owner account on an empty database — see
   [Users & permissions](#users--permissions)) plus whichever API keys from
   `.env.example` you use.
4. Generate a public domain under Settings → Networking.

To bring your local data along, upload `db.sqlite` into the volume once
(e.g. `railway ssh`, then copy it to `/data/db.sqlite`) before using the app.

Note: after the first boot, campaign YAML on the volume is the source of
truth — changes to `campaigns/` in git won't overwrite it. Delete
`/data/campaigns` and redeploy to re-seed.

## Architecture

```
agency-os/
├── core/           # Pipeline engine, plugin registry, DB, CLI, models
├── plugins/
│   ├── prospect_sources/   # WHO to sell to (IRS, SOS, OIA, MIV scrapers)
│   ├── products/           # WHAT you're selling (u9itus voter guide, etc.)
│   ├── channels/           # HOW you reach them (Smartlead, SMTP, manual)
│   └── enrichers/          # Contact enrichment (Apollo, Hunter)
├── campaigns/      # One folder per sales effort — campaign.yaml + scripts/
├── data/           # Scraped prospect data, exports
├── config.yaml     # Global config (API keys, DB path, schedule)
└── agency_os.py    # Entry point
```

## How it works

1. **Sync** — Prospect source plugins scrape/discover organizations and upsert them into the database
2. **Enrich** — Enricher plugins find contact names/emails for each prospect
3. **Enqueue** — The pipeline finds due follow-ups, loads the right email script, personalizes it, and sends via a channel plugin
4. **Stale check** — Prospects with no contact in N days move to "nurture" stage
5. **Digest** — Weekly pipeline summary (sent, opened, replied, stage breakdown)

## Adding a new product (e.g., consulting services)

1. Create `plugins/products/consulting_services.py` with a class that has `key`, `describe_value()`, `generate_demo_link()`, and `pricing_tiers()`
2. Create `campaigns/consulting-chambers/campaign.yaml` pointing to your new product + prospect sources + channels
3. Add email scripts in `campaigns/consulting-chambers/scripts/`
4. Run `python agency_os.py sync --campaign consulting-chambers`

No core code changes. The plugin auto-discovers.

## Adding a new prospect source

1. Create `plugins/prospect_sources/my_source.py` with a class that has `key`, `is_configured()`, and `discover(filters) -> Iterator[Prospect]`
2. Reference it in a campaign's `prospect_sources` list
3. Run sync

## Adding a new channel

1. Create `plugins/channels/my_channel.py` with `key`, `is_configured()`, and `send(recipient, subject, body, metadata) -> SendResult`
2. Reference it in a campaign's `channels` list

## Web Dashboard

```bash
pip install -r requirements.txt
python3 -m web.app
```

Open http://localhost:8000 and sign in. On a fresh database, create the first
owner account first:

```bash
python agency_os.py users create-owner --email you@example.com
```

| Page | What you can do |
|---|---|
| **Dashboard** | Pipeline stats across all campaigns — stage breakdown, open/reply rates |
| **Prospects** | Search, filter by source/stage, sort by name/revenue/city. Click any prospect for detail |
| **Prospect Detail** | View org info, outreach timeline, email history. Edit contact info. Move between stages |
| **Campaigns** | View campaign config — sources, channels, enrichers, cadence |
| **Emails** | Email log with status filter (sent, opened, replied, bounced) |
| **/api/stats** | JSON API for external dashboards |

```bash
# Monthly: refresh prospect lists
python agency_os.py sync --all

# Daily: enrich contacts
python agency_os.py enrich --all --limit 50

# Daily: enqueue due follow-ups
python agency_os.py enqueue --all --limit 50

# Daily: move stale prospects to nurture
python agency_os.py stale --all

# Weekly: pipeline digest
python agency_os.py digest --all
```

## Users & permissions

The dashboard is multi-user with role-based permissions, modeled on the
u9itus.dev staff permission system. Everyone shares one workspace (same
campaigns and prospects); roles control what each person can see and do.

- **Permissions** are a fixed catalog defined in code (`core/access.py` →
  `CATALOG`), e.g. `prospects.edit`, `calls.log`, `templates.edit`.
- **Roles** are named sets of permissions that owners create and edit at
  **Team → Roles**. A user's access is the union of all of their roles.
  Starter roles: Caller, Sales Rep, Template Editor, Viewer. Edit or delete
  them freely; restarts never overwrite your changes.
- **Owner** is a protected role with every permission plus team
  administration (users, roles, audit log). The app refuses any change that
  would leave no active owner.
- **New users get no access** until an owner gives them a role.
- **Every route is listed in `access.ROUTE_RULES`.** A route that isn't
  listed is denied for everyone, owners included. `tests/test_access.py`
  fails if a route is added without a rule.
- Changes apply on the user's next request. Deactivating a user signs them
  out immediately.
- Sign-ins, team changes, and prospect/template edits are recorded in
  **Team → Audit log**. Logged calls are attributed to the signed-in user.

Recovery from the server shell (on Railway, use `railway ssh` and pass
`--db /data/db.sqlite`):

```bash
python agency_os.py users list
python agency_os.py users create-owner --email you@example.com
python agency_os.py users grant-owner --email someone@example.com   # re-promote + reactivate
python agency_os.py users set-password --email someone@example.com
```

Run the access tests with `python -m pytest tests/`.

## API keys

Set via environment variables:

```bash
export SMARTLEAD_API_KEY=...
export APOLLO_API_KEY=...
export HUNTER_API_KEY=...
export SMTP_HOST=smtp.gmail.com
export SMTP_PORT=587
export SMTP_USER=...
export SMTP_PASS=...
export SMTP_FROM=...
```

## Current plugins

| Type | Key | Description |
|---|---|---|
| prospect_source | `irs_bmf` | IRS Exempt Organizations BMF — CA nonprofits |
| prospect_source | `sos_partners` | CA Secretary of State voter engagement partners |
| prospect_source | `oia_grantees` | LA County Office of Immigrant Affairs CBO grantees |
| prospect_source | `miv_partners` | Mobilize the Immigrant Vote CA partner CBOs |
| product | `u9itus_voter_guide` | u9itus digital voter guide platform |
| channel | `email_smartlead` | Smartlead API cold email |
| channel | `email_smtp` | Direct SMTP email |
| channel | `manual` | Log a manual touch (phone, in-person) |
| enricher | `local_scraper` | Local web scraper — finds websites, phones, emails (no API key needed) |
| enricher | `apollo` | Apollo.io contact enrichment |
| enricher | `hunter` | Hunter.io email finder + verifier |

## License

MIT