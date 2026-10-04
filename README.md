# agency-os

A plugin-driven, evergreen sales outreach engine. Find prospects, enrich contacts,
send sequenced emails, track pipeline progress, and generate weekly digests — for
any product or service.

## Quick start

Data lives in PostgreSQL. Point `DATABASE_URL` at a database (tables are
created on first use):

```bash
cd agency-os
pip install -r requirements.txt
createdb agency_os && export DATABASE_URL=postgresql://localhost/agency_os
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
  -e DATABASE_URL=postgresql://user:pass@host.containers.internal/agency_os agency-os
```

1. Create a Railway project from this GitHub repo.
2. **Add a PostgreSQL service** to the project (+ New → Database → PostgreSQL).
   On the web service, set `DATABASE_URL=${{Postgres.DATABASE_URL}}` (a
   reference variable, so it follows the database's credentials). The web
   service needs no volume; everything, including campaign files, is stored
   in Postgres and survives redeploys.
3. Set variables: `AGENCY_OS_OWNER_EMAIL` and `AGENCY_OS_OWNER_PASSWORD`
   (creates the first owner account on an empty database — see
   [Users & permissions](#users--permissions)) plus whichever API keys from
   `.env.example` you use.
4. Generate a public domain under Settings → Networking.

To bring data over from an old SQLite `db.sqlite`, import it once into the
empty database **before** the first boot creates an owner (the import refuses
to run if users or prospects already exist). From your machine, using the
Postgres service's public URL (`DATABASE_PUBLIC_URL`):

```bash
DATABASE_URL='postgresql://...' python agency_os.py import-sqlite --from db.sqlite
```

Campaign files: on first use the database is seeded from `campaigns/` in the
repo, then edits made in the dashboard are kept. Files added to `campaigns/`
in git are picked up on the next start; files already in the database are
not overwritten by git changes.

## Architecture

```
agency-os/
├── core/           # Pipeline engine, plugin registry, DB, CLI, models
├── plugins/
│   ├── prospect_sources/   # WHO to sell to (IRS, SOS, OIA, MIV scrapers)
│   ├── products/           # WHAT you're selling (u9itus voter guide, etc.)
│   ├── channels/           # HOW you reach them (Smartlead, SMTP, Twilio SMS, manual)
│   ├── enrichers/          # Contact enrichment (Apollo, Hunter)
│   └── schedulers/         # Meeting booking (Calendly)
├── campaigns/      # One folder per sales effort — campaign.yaml + scripts/
├── data/           # Scraped prospect data, exports
├── config.yaml     # Global config (API keys, DB path, schedule)
└── agency_os.py    # Entry point
```

## How it works

1. **Sync** — Prospect source plugins scrape/discover organizations and upsert them into the database
2. **Enrich** — Enricher plugins find contact names/emails for each prospect
3. **Enqueue** — The pipeline finds due follow-ups, loads the right email script, personalizes it, and sends via a channel plugin
4. **Bookings** — Scheduler plugins pull booked meetings and move those prospects to `demo_scheduled` (out of the automated sequence)
5. **Stale check** — Prospects with no contact in N days move to "nurture" stage
6. **Digest** — Weekly pipeline summary (sent, opened, replied, stage breakdown)

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

Channels are tried in order; one that can't reach a contact (e.g. SMS with no
phone number) returns `skipped` and the next channel is tried. A cadence step
can override the campaign's channels:

```yaml
cadence:
  - touch: 2
    delay_days: 4
    script: 02_sms_nudge        # SMS scripts only need a `body`
    next_stage: contacted
    channels: [sms_twilio, email_smtp]
```

## SMS (Twilio)

Set `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, and `TWILIO_FROM_NUMBER` (or
`TWILIO_MESSAGING_SERVICE_SID`), then add `sms_twilio` to a campaign's (or a
step's) `channels`. Texts go to the outreach row's `contact_phone`; US numbers
are normalized to E.164. Only the script `body` is sent (max 1,600 chars).
US business texting requires A2P 10DLC registration in Twilio; Twilio handles
STOP/HELP opt-outs.

## Booking meetings (Calendly)

Add `scheduler: calendly` to `campaign.yaml` and set `CALENDLY_SCHEDULING_URL`.
Templates can then use `{{booking_link}}` — a per-prospect link that prefills
their name/email and is tagged with the outreach ID.

With `CALENDLY_API_TOKEN` set, sync bookings into the pipeline:

```bash
python agency_os.py bookings --campaign voter-guide-cbo [--days 30] [--dry-run]
python agency_os.py bookings --all
```

Bookings match by the link's outreach tag, then by email. A booking moves the
prospect to `demo_scheduled` (never back from `proposal_sent` or closed) with
the meeting as its next follow-up; a cancellation moves it back to `engaged`.
Only prospects in `cold` or a cadence `next_stage` get automated touches, so
booked prospects stop receiving the sequence.

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

### Adding a rep

Send them a welcome email with a one-time link to set their own password
(no password ever goes over email). The command creates the user if needed:

```bash
python agency_os.py users invite --email rep@example.com --name "Jane Rep" --role "Sales Rep"
python agency_os.py users invite --email rep@example.com            # resend: new link, old one dies
python agency_os.py users invite --email rep@example.com --no-send  # print the email instead
```

The link expires after 7 days and works once. It points at
`AGENCY_OS_BASE_URL` (or Railway's public domain; override with `--base-url`)
and is sent over SMTP (`SMTP_HOST`, `SMTP_USER`, `SMTP_PASS`, `SMTP_FROM`).
Repeat `--role` to give several roles. Re-inviting an existing user leaves
their roles alone, so it doubles as a password reset link.

Recovery from the server shell (on Railway, `railway ssh`; `DATABASE_URL` is
already set there):

```bash
python agency_os.py users list
python agency_os.py users create-owner --email you@example.com
python agency_os.py users grant-owner --email someone@example.com   # re-promote + reactivate
python agency_os.py users set-password --email someone@example.com
```

Run the tests against a scratch database (it is wiped, and its name must
contain "test"): `TEST_DATABASE_URL=postgresql://localhost/agency_os_test python -m pytest tests/`.

## Lead packages (x402)

Buy lead lists from approved providers and pay in USDC over
[x402](https://x402.org). Unlocking a package pays once and imports its
leads into a campaign. The first time a package lead is contacted, the
pipeline pays that lead's royalty before sending. Every payment is recorded
in the `spend` table with its onchain transaction.

A lead's value is the contact that has really happened. Each lead carries its
contact history, and its **tier** is the deepest step that history proves
(`core/contact_depth.py`): mailed → emailed → phone verified → connected →
pitched. The tier is computed from the history, never taken from the seller's
claim. Each package guarantees that 90% of its leads reach a stated tier, and
each lead's royalty follows its own tier (`royalty_by_tier` in the catalog).
Call outcomes include the dispositions the guarantee relies on (disconnected,
answering machine, hung up, busy, fax tone, wrong number).

Off by default. To try it on Base Sepolia (test USDC):

1. `pip install -r requirements-payments.txt` (Docker: `--build-arg WITH_PAYMENTS=1`)
2. Set `AGENCY_OS_X402=on`, `AGENCY_OS_LEAD_PROVIDERS=https://provider.example`
   and the Coinbase CDP wallet keys (`CDP_API_KEY_ID`, `CDP_API_KEY_SECRET`,
   `CDP_WALLET_SECRET`). Fund the wallet with test USDC.
3. In **Admin → Campaign Settings**, turn on *Lead Packages* for a campaign and
   set its caps and monthly budget.
4. Give buyers the `packages.buy` permission and an allowance:
   `python agency_os.py spend allowance --email rep@example.com --usd 50`
5. Unlock from **Campaigns → Lead Packages**, or
   `python agency_os.py packages unlock --campaign ... --provider ... --package ... --email ...`

Safeguards:
- Only allowlisted https providers can be used.
- A quote is only paid if it is USDC on the campaign's network, goes to the
  catalog's pay-to address, and costs no more than the catalog price.
- Per-unlock and per-royalty caps, a campaign monthly budget and a per-user
  allowance all apply. Budget is reserved under a per-campaign lock, so
  parallel runs can't overspend.
- Each unlock and royalty can be paid only once.
- Mainnet needs `AGENCY_OS_X402_ALLOW_MAINNET=1` as well.
- Package leads never overwrite existing prospects. They are only contacted in
  the campaign they were unlocked into, and they're only texted if the package
  includes SMS consent.

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
export TWILIO_ACCOUNT_SID=...
export TWILIO_AUTH_TOKEN=...
export TWILIO_FROM_NUMBER=+1...
export CALENDLY_SCHEDULING_URL=https://calendly.com/you/demo
export CALENDLY_API_TOKEN=...
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
| channel | `sms_twilio` | Twilio SMS |
| channel | `manual` | Log a manual touch (phone, in-person) |
| enricher | `local_scraper` | Local web scraper — finds websites, phones, emails (no API key needed) |
| enricher | `apollo` | Apollo.io contact enrichment |
| enricher | `hunter` | Hunter.io email finder + verifier |
| scheduler | `calendly` | Calendly booking links + booking sync |

## License

MIT