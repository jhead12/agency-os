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

Open http://localhost:8000

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