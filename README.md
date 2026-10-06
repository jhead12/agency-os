# agency-os

A plugin-driven, evergreen sales outreach engine. Find prospects, enrich contacts,
send sequenced emails, track pipeline progress, and generate weekly digests — for
any product or service.

## Quick start

Data lives in PostgreSQL. Point `DATABASE_URL` at a database (tables are
created on first use). Settings can go in a `.env` file in the project folder
(copy `.env.example`); the CLI and the web app read it at startup, and anything
already set in your shell takes precedence. Campaigns can be named by a short
form such as `voter-guide-cbo` when it matches only one campaign.

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

Every command and option is listed in [CLI_REFERENCE.md](CLI_REFERENCE.md).
Introspection and environment-check helpers live in [tools/](tools/README.md)
(`python -m tools.doctor`, `python -m tools.inspect all`). A documentation wiki
(guides, concept explainers, per-plugin reference) is maintained at
`~/wiki/agency-os` — serve it locally with `python -m tools.wiki_serve`.

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
5. After the first deploy, make yourself a Super Admin so you can create Owners
   (`railway ssh`, then `python agency_os.py users grant-super-admin --email you@example.com`).

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

Open http://localhost:8000 and sign in. If 8000 is taken, the dashboard uses the
next free port (8001, 8002, ...) and prints the address; set `PORT` to choose one
yourself (an explicit `PORT` is never changed). On a fresh database, create the first
owner account first, and make it a Super Admin:

```bash
python agency_os.py users create-owner --email you@example.com
python agency_os.py users grant-super-admin --email you@example.com
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
  Starter roles: Caller, Sales Rep, Recruiter, Template Editor, Viewer. Edit or delete
  them freely; restarts never overwrite your changes.
- **Owner** is a protected role with every permission plus team
  administration (users, roles, audit log). The app refuses any change that
  would leave no active owner.
- **Super Admin** is a protected role above Owner. Only a Super Admin can
  grant or remove Owner or Super Admin, and Owners can't change a Super
  Admin's account. This is enforced in the database (`Database._guard_protected`),
  so it holds in the dashboard, the console and the remote CLI alike. The
  server-side `users` commands can always do it, for bootstrap and recovery.
- **New users get no access** until an owner gives them a role.
- **Campaign members** split the work: a campaign with no members is seen by
  everyone whose role allows it; once an Owner adds people or roles under
  **Campaign Settings → Who works this campaign** (or `campaigns assign` in the
  console), only they see it and its leads.
- **Campaign Owners:** a Super Admin can give a campaign to specific Owners
  (**Owners of this campaign** on its settings page, or `campaigns add-owner`).
  Other Owners then don't see it anywhere, and packages they publish never
  include its leads. Super Admins always see every campaign.
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
already set there). These skip permission checks and are logged as `cli`:

```bash
python agency_os.py users list
python agency_os.py users create-owner --email you@example.com
python agency_os.py users grant-owner --email someone@example.com        # re-promote + reactivate
python agency_os.py users grant-super-admin --email you@example.com      # first Super Admin
python agency_os.py users set-password --email someone@example.com
```

### Command console (browser and remote CLI)

Give a role the `cli.use` permission and its members get **Account → Console**
(`/console`), a terminal in the browser. It is **not a server shell**: each
line runs an agency-os command as the signed-in user, with that user's
permissions, so `help` lists only what their role allows. Changes ask
`Run this? [y/N]` first and are recorded in the audit log under the person.

```
users invite --email jane@example.com --name "Jane" --role Caller
users create-owner --email bob@example.com        # Super Admins only
users set-roles --email jane@example.com --role "Sales Rep"
search-prospects --q "food bank" --limit 5
set-stage --prospect-id 42 --stage engaged
```

The same commands run from your own terminal against a deployed server. Create
a CLI key under **Account → Command console**, then:

```bash
python agency_os.py connect --url https://your-app.up.railway.app --key aos_cli_...
python agency_os.py remote users list     # one command (--yes skips the prompt)
python agency_os.py remote                # interactive
```

The remote CLI only needs this repo and Python, not database access. CLI keys
(`aos_cli_...`) work only on the console endpoint (`POST /api/console`), not on
`/mcp` or `/api/tools`, and stop working when revoked or when the user is
deactivated.

Commands are generated from the tool registry (`core/tools.py`, parsed by
`core/console.py`): add a tool there and it appears in the console, the remote
CLI and, unless it's marked console-only, AI assistants (WebMCP/MCP). The team
administration tools are console-only, so AI assistants never see them. Full
command list: [CLI_REFERENCE.md](CLI_REFERENCE.md#command-console-browser-and-remote-cli).

Run the tests against a scratch database (it is wiped, and its name must
contain "test"): `TEST_DATABASE_URL=postgresql://localhost/agency_os_test python -m pytest tests/`.

## Tutorials & workflows

**Account → Tutorials & workflows** plays a workflow on your own screen, like a
visible Playwright: a cursor moves to each element, fields are typed into, pages
change, console commands run, and a caption explains each step. Built-in
tutorials (`workflows/tutorials/`) show each role how to use the app; users save
their own workflows, and back them up with Export/Import (one JSON file).

Workflows are data, never code (`core/workflows.py` validates them; the player is
`web/static/player.js`), which keeps sharing them safe and is the basis for a
future marketplace: a workflow can only do what the person playing it could do
by hand, can only open this app's pages, and anything that changes data asks
first. The format and step list are in
[CLI_REFERENCE.md](CLI_REFERENCE.md#tutorials--workflows).

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
5. Unlock from **Campaigns → Lead Packages**. *Review unlock* shows the price,
   the most the royalties can cost, and what's left in the campaign budget and
   your allowance before you pay. From the CLI:
   `python agency_os.py packages unlock --campaign ... --provider ... --package ... --email ... [--dry-run]`

To pause a package, untick it in the campaign editor. Its leads aren't
contacted, and no royalties are paid, until you tick it again. Spend shows on
the Campaigns page, your Account page and in `digest`.

**The guarantee.** Each package lead is judged from your own outreach
(`core/verify.py`). No single event fails a lead; evidence is weighed and a
lead fails at a score of 1.0:

| Evidence | Weight |
|---|---|
| Disconnected number, fax tone | 1.0 |
| Wrong number, once reported twice (or by two callers) | 1.0 |
| No answer / busy on 6 calls, never reached | 0.5 |
| Email bounced, mail returned | 0.5 each |
| Enricher finds a different email | 0.5 after a bounce, else 0.25 |
| AI review says not real (optional) | 0.5 |
| Seller's history proves less than the promised tier | 1.0 |
| Reached the organization / enricher agrees / AI says real | −1.0 / −0.25 / −0.5 |

These are the defaults. The rules are part of the deal: a package can state
its own terms (`guarantee.rules` in the catalog), a campaign sets defaults
for packages that don't (*Guarantee rules for new unlocks* in the campaign
editor: the fail score, how many wrong-number reports and no-answer calls
count, the window, the claim period after it, and how unworked leads count
when the window closes; weights in `campaign.yaml` under
`lead_packages.guarantee_rules.weights`). They're fixed on each package when
it's unlocked and shown before you pay. Every value is range-checked, and a
disconnected number, a fax tone, or a history below the promised tier always
fails a lead on its own.

Bounces and returned mail are recorded automatically from webhooks (or by
hand on the prospect page):

- **Lob**: add a webhook in the Lob dashboard for the `*.returned_to_sender`
  events pointing at `<your agency-os URL>/webhooks/lob`, and set
  `LOB_WEBHOOK_SECRET` to its secret. Requests are signature-checked.
- **Smartlead**: add a webhook for `EMAIL_BOUNCE` pointing at
  `<your agency-os URL>/webhooks/smartlead?key=<AGENCY_OS_WEBHOOK_KEY>`
  (Smartlead doesn't sign webhooks, so the URL carries a secret key).
- **Any other sender**: `POST /webhooks/bounce?key=<AGENCY_OS_WEBHOOK_KEY>`
  with `{"email": "...", "type": "hard", "id": "<event id>"}`.

Each endpoint is off until its secret is set. Soft bounces are ignored and
each event is recorded once.

Mark bounces and returned mail on the prospect page. A bounced email shows
*Find a new email*, which re-runs the campaign's enrichers and compares what
they find with the package's email. Failed leads are never contacted, so no
royalty is paid on them.

Each unlocked package has a guarantee page (**Lead Packages → Unlocked**, or
`packages verify --campaign ...`) with every lead's verdict and evidence. The
guarantee is broken once failures make 90% impossible
(shortfall = ⌈0.9 × leads⌉ − (leads − failed)). Within the verification window
(30 days unless the package says otherwise) you can **File claim**
(`packages claim --id N --email ...`). The provider answers with replacement
leads, a refund (recorded as a pending `refund_in` until you confirm it onchain
with `spend resolve`), or a dispute. Each package and seller shows its verified
rate measured across the unlocks on this server.

The AI review is off unless `AGENCY_OS_AI_REVIEW=on` and an AI model is
configured (see *AI agents* below; Claude or a local model). It reads call dispositions and notes for
leads near the threshold, counts as one signal, and runs only from *Re-check
leads* or `packages verify --ai`. A verdict is cached until the lead's evidence
changes.

If a payment goes out but the provider never answers, it stays **pending** and
keeps counting against the budget. Check it onchain, then close it:
`python agency_os.py spend pending`, then
`spend resolve --id N --status settled --tx 0x...` (or `--status failed`, which
frees the budget and lets it be paid again). If a pending unlock had in fact
settled, ask the provider to resend the leads.

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

### Selling our own lists

agency-os is also an x402 lead provider, so other agency-os instances (or any
x402 lead buyer) can buy from you. **Administration → Sell Lead Packages**
(permission `packages.sell`, owners by default) publishes a saved prospect
list as a package: pick the contact depth it guarantees, the unlock price,
royalties by tier and the guarantee window, check the list (it shows how many
prospects qualify), then publish.

- Only leads we can stand behind go in: a contact on file, no bounced email,
  and our own contact history at or above the promised tier. Leads marked
  **do not sell** on their prospect page, and leads bought from someone else,
  are never sold. Leads are re-checked at every sale.
- Buyers get the leads and a private claim token. Royalties are charged only
  for leads sold to that buyer, once each.
- Claims are checked against our records (a lead we've reached since the sale
  isn't dead) and capped at what breaks 90%. Replacements come from the same
  list; what can't be replaced waits on the Selling page for an owner to
  refund by hand and record the transaction. Nothing is refunded automatically.
- Income and refunds go in the same `spend` ledger (`unlock_in`, `royalty_in`,
  `refund_out`).

Turn the store on with `AGENCY_OS_SELL=on` and `AGENCY_OS_SELL_PAY_TO=<your
wallet address>` (and `pip install -r requirements-payments.txt`). Payments are
verified and settled by an x402 facilitator: `AGENCY_OS_X402_FACILITATOR`,
default `https://x402.org/facilitator`, which handles Base Sepolia. Mainnet
needs `AGENCY_OS_SELL_NETWORK=base`, `AGENCY_OS_X402_ALLOW_MAINNET=1` and a
mainnet facilitator.

**Rep royalties.** The reps who built a lead share in what buyers pay for it:
the package's contributor share (set when publishing, default 20%) of each
lead's part of the unlock and of its royalty, split *found the contact* 50%,
*reached the decision-maker* 30%, *sourced it* 20%. Credit comes from our
records (the rep whose contact edit supplied the email or phone; the rep who
logged a call that reached the decision-maker); owners add *sourced* credit,
or adjust any credit, on the prospect page. Shares become payable once the
buyer's guarantee period ends without a claim, and are taken back if a claim
on that lead holds. Reps see theirs under **Account → My data royalties** and
set a payout wallet (password required). Owners pay from **Administration →
Rep Payouts**: by hand (record the transaction), or with
`AGENCY_OS_PAYOUTS=on` from the CDP wallet, one confirmed payout at a time,
above `AGENCY_OS_PAYOUT_MIN_USD` (default $5) and up to
`AGENCY_OS_PAYOUT_MAX_USD` (default $500).

**Testnet trial (dev provider).** Run two copies, each with its own
`DATABASE_URL`. On the seller: `AGENCY_OS_SELL=on`, a test wallet in
`AGENCY_OS_SELL_PAY_TO`, publish a list. On the buyer: the CDP wallet keys
with test USDC, `AGENCY_OS_X402=on`, and
`AGENCY_OS_LEAD_PROVIDERS=http://127.0.0.1:<seller port>/x402`. Unlock from the
buyer's Lead Packages page and check both transactions on Sepolia Basescan.

Selling contact data can make you a California data broker under the Delete
Act (registration and deletion requests); keep a consent note on each package.

## AI agents (beta, opt-in)

Each user chooses whether to use AI. It's off by default, and anyone who
leaves it off sees the app exactly as before. Turn it on under **Account → AI
features**. That needs the `agents.use` and/or `ai.connect` permission (the
Sales Rep starter role has both on new installs; on an existing install, add
them to roles under Admin → Roles). `AGENCY_OS_AI=off` hides AI for everyone.

- **Ask an agent** (prospect page): sales personas from
  [agency-agents](https://github.com/msitarzewski/agency-agents) (`agents/`)
  draft the next email, a text, call prep, a MEDDPICC deal review, or a
  proposal outline from the prospect's record. Drafts only; nothing is sent.
  *Save as note* adds the draft to the prospect.
- **Your own AI (WebMCP)**: a browser AI that supports WebMCP
  (`document.modelContext`, currently a Chrome origin trial) gets agency-os
  tools: search and read prospects, calls and campaigns, draft with a persona,
  and add notes, log calls or change stages. Every call runs as you with your
  permissions; any change opens a confirm dialog showing exactly what will
  change, and is recorded in the audit log.

The model (`core/llm.py`):

- **Claude**: set `ANTHROPIC_API_KEY` and `pip install -r requirements-ai.txt`
  (Docker: `--build-arg WITH_AI=1`). Uses `claude-opus-5-5`.
- **Local model, e.g. Hermes on Ollama**: `ollama pull hermes3`, then
  `AGENCY_OS_LLM=openai_compatible`, `AGENCY_OS_LLM_BASE_URL=http://localhost:11434/v1`,
  `AGENCY_OS_LLM_MODEL=hermes3`. Any OpenAI-compatible server works. A hosted
  deploy can't reach a model on your laptop; use this when running agency-os
  yourself.

**Connect any MCP client (Hermes Agent, Claude Desktop/Code, Rook...).**
agency-os is an MCP server at `<your agency-os URL>/mcp` (streamable HTTP). With
AI features on, **Account → Connect an AI app** creates a personal token,
shown once, read-only unless you tick *Allow changes*, and gives ready-made
config:

```yaml
# Hermes Agent: ~/.hermes/config.yaml
mcp_servers:
  agency_os:
    url: https://your-agency-os.example/mcp
    headers:
      Authorization: "Bearer aos_pat_..."
```

```bash
claude mcp add --transport http agency-os https://your-agency-os.example/mcp \
  --header "Authorization: Bearer aos_pat_..."
```

The server offers the same tools as WebMCP (changes only for tokens allowed to
make them; your client's approval prompt is the confirmation, and every change
is audited as `mcp:<token name>`), one prompt per persona and task so your own
model does the drafting, and personas and prospects as resources. Hermes can
run on a local model, so this is how a local AI works with a hosted agency-os.

**Claude.ai and ChatGPT connectors.** Add a custom connector with the same
`/mcp` URL. The app registers itself and sends you to an agency-os consent page
(behind your normal sign-in) where you allow it, optionally with changes.
This is standard OAuth 2.1: PKCE, single-use codes, one-hour access tokens,
rotating 30-day refresh tokens. Revoke any token or app on your Account page.
Set `AGENCY_OS_BASE_URL` to the public https URL (Railway's domain is used
automatically) so the OAuth metadata points to the right place.

Tokens and connected apps stop working when their user turns AI features off,
is deactivated, or when `AGENCY_OS_AI=off`.

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