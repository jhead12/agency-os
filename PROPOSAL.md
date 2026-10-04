# agency-os
## Sales Outreach Engine for Civic Technology — Business Plan & Product Guide

**Prepared by:** Joshua Head, Founder, U9itus
**Date:** October 2026
**Product:** U9itus Digital Voter Guide Platform
**System:** agency-os — Plugin-Driven Sales Outreach & CRM

---

## 1. Executive Summary

U9itus is a digital voter guide platform that helps community organizations distribute nonpartisan candidate comparisons and ballot measure explanations to their constituents. The product is built, deployed, and live at **www.u9itus.com**.

**agency-os** is the sales engine that finds, contacts, and converts organizations into U9itus customers. It is a standalone, plugin-driven system built in Python that automates the entire outbound sales pipeline — from prospect discovery through email, phone, direct mail, demo scheduling, and close.

### The Opportunity

- **2,442 verified prospects** already loaded — community-based organizations (CBOs) across Los Angeles and California, sourced from IRS Business Master File, California Secretary of State partner lists, Office of Immigrant Affairs grantees, and Muslim Initiative for Values partner networks
- **Zero paid API dependencies** for prospect discovery or contact enrichment — the system uses public government data and a proprietary local web scraper
- **Multi-channel outreach**: email (Smartlead/SMTP), direct mail (Lob postcards & letters), SMS (Twilio), phone (call scripts + logging), and manual touch — all configurable per campaign
- **Product pricing** from $500 to $4,000 per election cycle, with a tiered structure designed for individual CBOs up to 10-organization coalitions

### Traction to Date

| Metric | Value |
|--------|-------|
| Prospects loaded | 2,442 |
| Active campaigns | 2 (CBO Los Angeles, Elected Officials California) |
| Plugins built | 15 across 5 categories |
| Dashboard pages | 25 web templates |
| Code base | ~10,500 lines of Python |
| Deployment | Railway (Docker, Postgres, persistent volumes) |
| Source control | GitHub (private, multi-branch) |

---

## 2. The Problem

### For Community Organizations

Community-based organizations want to help their constituents make informed voting decisions. But building a voter guide from scratch requires:

- Researching every candidate and ballot measure (hundreds of hours)
- Maintaining accuracy as positions change
- Translating into multiple languages (English, Spanish, Chinese, Korean, Tagalog)
- Building a mobile-friendly web presence
- Keeping it nonpartisan and compliant

Most CBOs lack the engineering capacity or budget to do this alone.

### For U9itus as a Business

U9itus solves the CBO's problem, but selling to CBOs at scale requires:

- **Finding the right organizations** — not all CBOs do civic engagement work
- **Reaching decision-makers** — executive directors, civic engagement leads
- **Demonstrating value quickly** — showing a personalized demo, not a generic pitch
- **Following up systematically** — CBOs are slow to respond; cadence matters
- **Tracking every interaction** — who was contacted, when, what was said, what stage they're in
- **Coordinating a sales team** — callers, reps, template editors, each with different access levels

Without a system, this is spreadsheets, sticky notes, and lost opportunities.

---

## 3. The Solution

### agency-os: A Plugin-Driven Sales Outreach Engine

agency-os is a complete sales operations platform that automates every step of the outreach pipeline. It is designed to be **evergreen** — new products, data sources, and channels can be added by dropping a single Python file into a plugins folder, with no changes to the core system.

### Architecture Overview

```
┌─────────────────────────────────────────────────────────┐
│                     agency-os                           │
│                                                         ││  ┌─────────────┐  ┌──────────┐  ┌──────────────┐   │
│  │  Prospect     │  │  Product  │  │   Channel    │   │
│  │  Sources      │  │  Plugins  │  │   Plugins    │   │
│  │               │  │           │  │              │   │
│  │ • IRS BMF     │  │ • U9itus  │  │ • Email      │   │
│  │ • SOS Partners│  │   Voter   │  │   (SMTP/     │   │
│  │ • OIA Grantees│  │   Guide   │  │   Smartlead) │   │
│  │ • MIV Partners│  │           │  │ • Direct Mail│   │
│  │               │  └──────────┘  │   (Lob)      │   │
│  └─────────────┘                │ • SMS (Twilio)│   │
│                                  │ • Phone       │   │
│  ┌─────────────┐  ┌──────────┐  │ • Manual      │   │
│  │  Enrichers   │  │Scheduler │  └──────────────┘   │
│  │              │  │          │                      │
│  │ • Local      │  │• Calendly│  ┌──────────────┐   │
│  │   Scraper    │  └──────────┘  │   Pipeline    │   │
│  │ • Apollo     │                │              │   │
│  │ • Hunter     │                │  cold →      │   │
│  └─────────────┘                │  contacted → │   │
│                                  │  engaged →   │   │
│  ┌────────────────────────┐     │  demo →      │   │
│  │   Web Dashboard         │     │  proposal →  │   │
│  │   (FastAPI + Jinja2)    │     │  closed_won  │   │
│  │                         │     └──────────────┘   │
│  │ 25 pages, 5 user roles  │                        │
│  └────────────────────────┘                        │
│                                                     │
│  Database: PostgreSQL (Railway)                     │
│  Deploy: Docker on Railway with persistent volume   │
└─────────────────────────────────────────────────────┘
                    │
                    ▼
           ┌──────────────┐
           │   U9itus     │
           │   Platform   │
           │              │
           │ www.u9itus.com│
           └──────────────┘
```

### How It Works — The Sales Pipeline

#### Step 1: Prospect Discovery

The system pulls real organization data from four public sources:

| Source | What it provides | Count |
|--------|-----------------|-------|
| IRS Business Master File | All registered 501(c)(3) nonprofits in California, filtered by NTEE code (civic, human services, education, public safety) and minimum revenue | 2,342 |
| CA Secretary of State Partners | Organizations officially partnered with the state for voter engagement | 14 |
| Office of Immigrant Affairs Grantees | OIA-funded organizations providing civic integration services | 18 |
| Muslim Initiative for Values Partners | MIV partner network organizations | 68 |

Each prospect record includes: organization name, EIN, NTEE classification, focus area, address, city, state, zip, county, annual revenue, website URL, and voter engagement indicators.

#### Step 2: Contact Enrichment

The system automatically discovers contact names, emails, and phone numbers for each prospect — **without paid API services**:

- **Local Scraper** (proprietary): Guesses the organization's domain from its name, falls back to DuckDuckGo search, then scrapes the website for contact information, staff directories, and leadership pages. Zero cost per prospect.
- **Apollo.io**: Integrated as a dormant slot — activates when an API key is provided.
- **Hunter.io**: Integrated as a dormant slot — activates when an API key is provided.

#### Step 3: Multi-Channel Outreach

Each campaign defines a **cadence** — a sequence of timed touches across multiple channels:

**Example: CBO Outreach Cadence (12-day sequence)**

| Touch | Day | Channel | Script | Purpose |
|-------|-----|---------|--------|---------|
| 0 | 0 | Email | Cold outreach | Introduce U9itus, show personalized value |
| 1 | 3 | Email | Follow-up: Impact | Share concrete outcomes, multilingual capabilities |
| 2 | 7 | Email + Direct Mail | Follow-up: Co-brand | Offer co-branded demo, postcard arrives |
| 3 | 12 | Email | Breakup | Final touch, create urgency |

Each touch is **personalized** using variables: organization name, contact first name, focus area, city, state, demo link, sender name, sender email. Scripts are written in YAML and can be edited in the dashboard without touching code.

#### Step 4: Demo Portal Provisioning

When a prospect shows interest (clicks the demo link, replies, or takes a meeting), the system can automatically provision a **personal demo portal** on U9itus:

- The prospect gets a co-branded page with their organization's name, their state's ballot measures and candidates
- A "Claim this page" link lets them take ownership and customize
- U9itus reports back when the prospect views or claims the page
- The pipeline stage auto-advances: viewed → engaged, claimed → demo scheduled, published → ready to close

This integration is built on the agency-os side (A1–A9 complete). The U9itus Laravel side (U1–U10) is the next development milestone.

#### Step 5: Pipeline Tracking & CRM

The dashboard provides full visibility into every prospect's journey:

- **7-stage pipeline**: Cold → Contacted → Engaged → Demo Scheduled → Proposal Sent → Closed Won / Closed Lost / Nurture
- **Stages only move forward** — the system never regresses a prospect
- **Activity log** on each prospect: every email, call, stage change, and note is recorded
- **Email log**: full sent email history with subject, body, and delivery status
- **Call log**: structured call recording with outcome, notes, and follow-up scheduling
- **Calendar**: visual month grid with all scheduled follow-ups, exportable as ICS

---

## 4. Product: U9itus Digital Voter Guide

### What CBOs Get

| Feature | Starter ($500) | Pro ($1,500) | Coalition ($4,000) |
|---------|---------------|-------------|-------------------|
| Co-branded voter guide with your logo | ✅ | ✅ | ✅ |
| Constituents reached | Up to 1,000 | Unlimited | Unlimited |
| Candidate comparisons side-by-side | ✅ | ✅ | ✅ |
| Ballot measures in plain language | ✅ | ✅ | ✅ |
| Multilingual support | — | Spanish, Korean, Chinese, Tagalog | Same + custom |
| Embed on your website | — | ✅ | ✅ |
| Printable PDF voter guides | — | ✅ | ✅ |
| Partner CBOs | 1 | 1 | Up to 10 |
| Custom branding per partner | — | — | ✅ |
| Analytics dashboard | — | — | ✅ |
| Account manager | — | — | ✅ |
| QR codes for print materials | — | — | ✅ |
| Support | Email | Priority | Dedicated |

**Pricing model**: Per election cycle (aligns revenue with the actual usage pattern — organizations need voter guides for primary and general elections)

### Why Organizations Buy

1. **Time**: Building a voter guide takes 200+ hours. U9itus does it in minutes.
2. **Credibility**: Nonpartisan, verified public records — not opinions or endorsements.
3. **Reach**: Mobile-first, multilingual, no account required for constituents.
4. **Brand**: Co-branded with the organization's logo — they own the distribution.
5. **Compliance**: Labeled as a sample, stays out of search engines, expires after the election.

---

## 5. Campaign Segmentation

agency-os supports **multiple campaigns**, each self-contained with its own prospects, scripts, plugins, and cadence:

### Active Campaigns

| Campaign | Target | Geography | Prospect Count | Cadence |
|----------|--------|-----------|----------------|---------|
| CBO Outreach (Los Angeles) | Community-based organizations | LA County, CA | 2,442 | 4-touch email + direct mail, 12 days |
| Elected Officials Outreach | City council members, supervisors | California statewide | 0 (pending sync) | 4-touch email, constituents angle, 21-day stale |

### Creating a New Campaign

An admin can create a new campaign through the dashboard:

1. Navigate to **Campaigns Admin** → create campaign folder
2. Add a `campaign.yaml` with product, data sources, channels, filters, and cadence
3. Write outreach scripts (email YAML, phone scripts, mail templates)
4. Associate plugins via the admin UI (checkboxes for sources, channels, enrichers; radio for product and scheduler)
5. Sync prospects from the selected data sources
6. Start the outreach pipeline

**No code changes required** — the plugin system auto-discovers everything.

---

## 6. Technology & Architecture

### Tech Stack

| Layer | Technology | Why |
|-------|-----------|-----|
| Language | Python 3.12+ | Best scraping/data ecosystem, fast iteration |
| Web Framework | FastAPI + Jinja2 | Async, fast, type-safe, auto-docs |
| Database | PostgreSQL (prod) / SQLite (dev) | ACID, reliable, scalable |
| Deployment | Docker on Railway | Auto-deploy from GitHub, persistent volumes |
| Source Control | GitHub (private) | Multi-branch, CI/CD via Railway |
| Plugin System | Python Protocol classes | Duck-typed, no inheritance, zero-coupling |

### Plugin System

Five plugin protocols, each auto-discovered from the `plugins/` directory:

| Protocol | Purpose | Plugins Built |
|----------|---------|--------------|
| **ProspectSource** | Discovers organizations to sell to | 4 (IRS BMF, SOS Partners, OIA Grantees, MIV Partners) |
| **Product** | Describes what you're selling, generates demos, pricing | 1 (U9itus Voter Guide) |
| **Channel** | Delivers outreach messages | 5 (Email SMTP, Email Smartlead, Direct Mail Lob, SMS Twilio, Manual) |
| **Enricher** | Finds contact names/emails/phones for prospects | 3 (Local Scraper, Apollo, Hunter) |
| **Scheduler** | Books meetings with prospects | 1 (Calendly) |

**Adding a new product** (e.g., a different SaaS): drop a `.py` file in `plugins/products/`, create a campaign YAML referencing it. Done.

**Adding a new channel** (e.g., LinkedIn outreach): drop a `.py` file in `plugins/channels/`. Done.

### Dashboard

25 web pages covering the full sales workflow:

- **Dashboard** — pipeline stats, campaign cards, recent activity
- **Prospects** — searchable, sortable, paginated list with campaign filter, saved lists, print/export
- **Prospect Detail** — full profile, editable org info, pipeline stage, activity log, portal status, call history
- **Campaigns** — campaign overview with YAML viewer
- **Campaigns Admin** — plugin association editor (owners only)
- **Plugins** — status of all 15 plugins, configured/needs-setup badges, env var checklist
- **Email Templates** — inline editor with live preview for any prospect
- **Mail Templates** — postcard/letter designer with visual 4×6 preview, Lob integration
- **Call Scripts** — phone call scripts with print view
- **Call Log** — structured call recording with outcomes
- **Emails** — sent email history with full body
- **Calendar** — visual month grid, ICS export
- **Admin: Users** — user management with invite links
- **Admin: Roles** — custom role builder with 13 permissions
- **Admin: Audit** — action log
- **Admin: Jobs** — scheduled job runner
- **Account** — password change, profile

### Access Control

5 starter roles with 13 permissions:

| Role | Can do |
|------|--------|
| **Owner** | Everything — manage users, roles, campaigns, plugins, audit |
| **Sales Rep** | View/edit prospects, move pipeline, log calls, view emails, manage demo portals |
| **Caller** | View prospects, log calls, move pipeline stages, view calendar |
| **Template Editor** | Edit email and mail templates, view campaigns |
| **Viewer** | Read-only access to everything except email bodies |

Custom roles can be created by picking from the 13-permission catalog.

### Data Persistence

- **Production**: PostgreSQL on Railway (separate database service, survives redeploys)
- **Campaign files**: Persistent volume mount at `/data` — admin edits to campaign configs survive redeploys
- **DB initialization**: Startup script creates tables if missing, seeds campaign files only if they don't already exist

---

## 7. Go-To-Market Strategy

### Phase 1: Los Angeles CBOs (Current)

- **Target**: 2,442 CBOs in LA County, filtered by civic engagement focus and $100K+ annual revenue
- **Channels**: Email (primary), direct mail postcards (touch 2), phone (warm prospects)
- **Goal**: 50 meetings booked → 15 demos → 5 closed deals at Starter tier ($2,500 revenue)
- **Timeline**: 60 days from first send

### Phase 2: California Elected Officials

- **Target**: City council members, county supervisors, school board members statewide
- **Angle**: Constituents deserve better voter info — position the voter guide as a constituent service
- **Goal**: 100 meetings → 20 demos → 8 closed deals ($4,000–$12,000 revenue at Pro/Coalition tier)

### Phase 3: Multi-State Expansion

- **Product**: Reuse agency-os with new prospect sources (other states' SOS data, national nonprofit registries)
- **Plugin**: New ProspectSource plugins for each state's public data
- **Timeline**: Q1 2027

### Revenue Model

| Scenario | Deals | Avg Price | Revenue |
|----------|-------|-----------|---------|
| Conservative (1% close rate) | 24 | $1,000 | $24,000 |
| Target (2% close rate) | 49 | $1,200 | $58,800 |
| Optimistic (3% close rate) | 73 | $1,500 | $109,500 |

*Per election cycle. Primary and general elections = 2 cycles/year.*

### Customer Acquisition Cost

| Item | Cost |
|------|------|
| Prospect data | $0 (public records) |
| Contact enrichment | $0 (local scraper) |
| Email sending | $0–$39/month (SMTP free, Smartlead $39/mo) |
| Direct mail | $0.615/postcard (Lob, pay per piece) |
| SMS | $0.0079/message (Twilio) |
| Infrastructure | $5/month (Railway) |
| Scheduling | $0 (Calendly free tier) |
| **Total monthly overhead** | **$5–$44** |

With 2,442 prospects and a 4-touch cadence, the variable cost per prospect is under $0.50 (email-only) or ~$1.50 (email + one postcard). At a $500 average deal, **CAC is under $150** even at a 1% close rate.

---

## 8. Integration: agency-os ↔ U9itus

The two systems are designed to work together as a closed loop:

```
agency-os finds prospect → sends email with demo link →
prospect clicks link → U9itus serves personalized demo portal →
prospect views/claims portal → U9itus reports event →
agency-os auto-advances pipeline stage →
sales rep calls to close →
U9itus provisions full account
```

### Integration Status

| Component | Status |
|-----------|--------|
| **A1**: U9itus API HTTP client (bearer auth, retry, timeout) | ✅ Built |
| **A2**: Product plugin — provision demos, pull events, portal status | ✅ Built |
| **A3**: CLI `provision` command — idempotent demo provisioning | ✅ Built |
| **A4**: CLI `pull-events` command — event feed with auto-stage-advance | ✅ Built |
| **A5**: DB tables — product_events, sync_cursors | ✅ Built |
| **A8**: Environment config — U9ITUS_BASE_URL, U9ITUS_AGENCY_TOKEN | ✅ Built |
| **A9**: Cold cadence fix — stages never move backward | ✅ Built |
| **U1–U10**: U9itus Laravel API endpoints | 🔲 Next milestone |

### What U1–U10 Will Deliver

On the U9itus side:
- `POST /api/v1/agency/demo-portals` — accept provisioning requests
- `GET /api/v1/agency/events` — return append-only event feed
- Organization claim flow for prospects
- Demo portal pages with `?src=outreach` tracking
- Portal expiration and SEO noindex

---

## 9. Competitive Advantage

### Why This System Wins

| Advantage | How |
|-----------|-----|
| **Zero-cost prospect data** | IRS BMF + government partner lists are public. Competitors pay Apollo ($49/mo+), ZoomInfo ($15K/yr), or manual research. |
| **Zero-cost enrichment** | Proprietary local scraper finds contacts from org websites. No Apollo/Hunter subscription required. |
| **Multi-channel by default** | Email + direct mail + SMS + phone in one system. Competitors are email-only (Smartlead, Instantly) or CRM-only (HubSpot). |
| **Product-aware** | The system knows what it's selling. It generates personalized demo links, describes value per prospect, and provisions actual demo accounts. Generic CRMs don't. |
| **Plugin-driven = evergreen** | New product? Drop a .py file. New state's data? Drop a .py file. New channel? Drop a .py file. No core code changes. |
| **Campaign segmentation** | Each campaign is self-contained — different scripts, channels, cadences, target segments. Not a one-size-fits-all blast. |
| **Built for civic tech** | NTEE code filtering, voter engagement indicators, multilingual messaging, co-branding — all first-class features, not afterthoughts. |
| **Lowest possible CAC** | Public data + free enrichment + pay-per-piece mail + $5/mo hosting = under $150 CAC at 1% close rate. |

---

## 10. Development Roadmap

### Completed

- ✅ Core architecture (plugins, pipeline, campaigns, database, CLI)
- ✅ 4 prospect source plugins (IRS BMF, SOS, OIA, MIV)
- ✅ 1 product plugin (U9itus Voter Guide)
- ✅ 5 channel plugins (Email SMTP, Email Smartlead, Lob Direct Mail, SMS Twilio, Manual)
- ✅ 3 enricher plugins (Local Scraper, Apollo, Hunter)
- ✅ 1 scheduler plugin (Calendly)
- ✅ Web dashboard (25 pages, responsive, mobile nav)
- ✅ Access control (5 roles, 13 permissions, audit log)
- ✅ Campaign segmentation (2 campaigns, self-contained)
- ✅ Admin campaign management (plugin association UI, YAML editor)
- ✅ Email template editor with live preview
- ✅ Mail template editor with postcard/letter visual preview
- ✅ Test email mode (test-send CLI + --test-email flag)
- ✅ U9itus integration client-side (A1–A9)
- ✅ Data persistence (PostgreSQL + Railway volumes)
- ✅ Deployment (Docker, Railway, GitHub CI/CD)

### Next Milestones

| Priority | Milestone | Description | Timeline |
|----------|-----------|-------------|----------|
| 1 | U9itus API endpoints (U1–U10) | Laravel API for demo provisioning + event feed | 2 weeks |
| 2 | First outreach send | Configure SMTP/Smartlead, run first campaign | 1 week |
| 3 | Call recording integration | Record phone calls and attach to prospect records | 2 weeks |
| 4 | Analytics dashboard | Conversion rates, channel performance, rep activity | 3 weeks |
| 5 | Multi-state expansion | New prospect sources for TX, NY, FL, AZ | 4 weeks |
| 6 | AI-powered personalization | GPT-generated email bodies from prospect data | 4 weeks |

---

## 11. Financial Summary

### Startup Costs

| Item | One-time cost |
|------|--------------|
| Development | $0 (founder-built) |
| Domain | $12/year (u9itus.com) |
| GitHub | $0 (private repos free) |
| Railway | $5/month (hobby plan) |
| Lob account | $0 (developer plan, pay per piece) |
| Twilio account | $0 (pay per message) |
| Calendly | $0 (free tier) |
| **Total startup** | **~$17** |

### Operating Costs (Monthly)

| Item | Cost | Scaling |
|------|------|---------|
| Railway hosting | $5/mo | Fixed |
| PostgreSQL | $5/mo (Railway) | Fixed |
| Email sending | $0–$39/mo | Per sender volume |
| Direct mail | $0.615/piece | Per mail piece sent |
| SMS | $0.0079/msg | Per message sent |
| Domain | $1/mo | Fixed |
| **Base monthly** | **$11–$50** | + variable per-outreach |

### Revenue Projection

Based on 2,442 prospects, 4-touch cadence, per election cycle:

| Scenario | Close Rate | Deals | Avg Deal | Revenue/Cycle | Revenue/Year (2 cycles) |
|----------|-----------|-------|----------|--------------|----------------------|
| Conservative | 1.0% | 24 | $1,000 | $24,000 | $48,000 |
| **Target** | **2.0%** | **49** | **$1,200** | **$58,800** | **$117,600** |
| Optimistic | 3.0% | 73 | $1,500 | $109,500 | $219,000 |

### Unit Economics

| Metric | Value |
|--------|-------|
| CAC (target scenario) | ~$120 |
| LTV (Starter, 1 cycle) | $500 |
| LTV (Pro, 2 cycles) | $3,000 |
| LTV (Coalition, 2 cycles) | $8,000 |
| Gross margin | ~95% (software, near-zero delivery cost) |
| Payback period | <1 cycle (60–90 days) |

---

## 12. Team & Operations

### Current Team

- **Joshua Head** — Founder & CEO. Product development, system architecture, sales strategy.

### Hiring Plan

| Role | When | Responsibility |
|------|------|---------------|
| Sales Caller | Phase 1 (Month 1) | Work the phone for warm/demo-scheduled prospects. agency-os Caller role — view prospects, log calls, move stages. |
| Sales Rep | Phase 2 (Month 3) | Full pipeline management. agency-os Sales Rep role — edit prospects, view emails, manage demo portals. |
| Template Editor | Phase 2 (Month 3) | A/B test email and mail templates. agency-os Template Editor role. |
| Developer (contract) | U1–U10 milestone | Build U9itus Laravel API endpoints for integration. |

### Operational Workflow

1. **Daily**: Cron job runs `enqueue` — sends due follow-up emails via configured channel
2. **Daily**: Cron job runs `pull-events` — syncs portal views/claims, auto-advances stages
3. **Weekly**: Sales caller works the call log — calls all "engaged" and "demo_scheduled" prospects
4. **Weekly**: Owner reviews dashboard — pipeline stats, stale prospects, conversion rates
5. **Monthly**: Template editor reviews email performance, A/B tests new scripts
6. **Monthly**: Owner reviews campaign config — adjusts cadence, adds/removes channels

---

## 13. Risk & Mitigation

| Risk | Likelihood | Impact | Mitigation |
|------|-----------|--------|------------|
| Low email deliverability | Medium | High | Smartlead warmup + SPF/DKIM/DMARC setup; start with small batches |
| CBOs don't respond to cold email | Medium | High | Multi-channel: direct mail postcards have 4× higher response rate than email alone |
| U9itus portal integration delayed | Medium | Medium | System works without it — demo links fall back to generic /compare page |
| Prospect data quality issues | Low | Medium | IRS BMF is authoritative; local scraper validates websites; enrichment is optional |
| Competitor enters civic tech sales | Low | Low | Plugin architecture means we can pivot to any product; data moat is the 2,442 verified prospects |
| Railway downtime | Low | Medium | Railway 99.9% SLA; DB is PostgreSQL with automated backups |

---

## 14. Appendix

### A. File Structure

```
agency-os/
├── agency_os.py              # CLI entry point
├── config.yaml               # System config
├── requirements.txt          # Python dependencies
├── Dockerfile                # Container build
├── railway.json              # Railway deployment config
├── .env.example              # Environment variable template
├── CLI_REFERENCE.md          # Complete CLI command reference
├── core/                     # Core engine
│   ├── db.py                 # Database layer (PostgreSQL/SQLite)
│   ├── models.py             # Data models (Prospect, Outreach, etc.)
│   ├── protocols.py          # Plugin protocol definitions
│   ├── registry.py           # Plugin auto-discovery
│   ├── pipeline.py           # Outreach pipeline & cadence logic
│   ├── campaign.py           # Campaign config & discovery
│   ├── access.py             # Roles, permissions, auth
│   ├── cli.py                # CLI command implementations
│   ├── jobs.py               # Scheduled job runner
│   ├── migrate.py            # DB migrations
│   ├── welcome.py            # User invite/welcome emails
│   └── ics.py                # Calendar ICS generation
├── plugins/                  # Plugin system (auto-discovered)
│   ├── prospect_sources/     # 4 plugins
│   ├── products/             # 1 plugin + U9itus API client
│   ├── channels/             # 5 plugins
│   ├── enrichers/            # 3 plugins
│   └── schedulers/           # 1 plugin
├── campaigns/                # Campaign configs (self-contained)
│   ├── voter-guide-cbo/      # CBO campaign (LA, 2,442 prospects)
│   │   ├── campaign.yaml
│   │   └── scripts/          # 11 YAML scripts (email, phone, mail)
│   └── voter-guide-elected-officials/  # EO campaign (CA statewide)
│       ├── campaign.yaml
│       └── scripts/          # 4 YAML scripts
├── web/                      # FastAPI web dashboard
│   ├── app.py                # Routes & logic
│   ├── static/               # CSS, JS
│   └── templates/            # 25 Jinja2 templates
├── tests/                    # Test suite
└── docs/                     # Documentation
    └── U9ITUS_PORTAL_INTEGRATION.md
```

### B. CLI Commands

| Command | Purpose |
|---------|---------|
| `sync --campaign <name>` | Load prospects from configured data sources |
| `enrich --campaign <name>` | Find contact info for prospects missing email/phone |
| `enqueue --campaign <name>` | Send due follow-up emails |
| `enqueue --test-email <addr>` | Send campaign emails to a test address |
| `test-send --campaign <name> --to <email>` | Send a single test email |
| `provision --campaign <name>` | Provision demo portals on U9itus |
| `pull-events --campaign <name>` | Sync portal events, auto-advance stages |
| `bookings` | Sync Calendly bookings |
| `stale --campaign <name>` | Move stale prospects to nurture |
| `digest --campaign <name>` | Show pipeline digest |
| `campaigns` | List all campaigns |
| `plugins` | List all plugins |
| `users create-owner` | Create first admin user |
| `users invite` | Invite a new user with welcome email |
| `users list` | List all users |
| `users set-password` | Reset a user's password |

### C. Contact

**Joshua Head**
Founder, U9itus
joshua@u9itus.com
www.u9itus.com

---

*This document was generated from the agency-os codebase (github.com/jhead12/agency-os) and reflects the system as of October 2026. All metrics are live from the production database.*