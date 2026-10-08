# Operations runbook

How agency-os is run day to day: who does what, on which schedule, and the rules
that have to hold while doing it. It covers the work from finding a prospect,
through outreach and close, to billing and renewal.

The business case (market, pricing, revenue model) is in
[PROPOSAL.md](../PROPOSAL.md). This document is the part that turns it into a
routine. Every section marks what's **built**, what's **planned** (with its spec)
and what's still **only a process** run by people.

Status (2026-10-08): written for **Phase 1, LA CBOs**, with one founder and the
first caller hire. Revisit it when the first sales rep starts (PROPOSAL §12,
month 3).

## 1. Roles

The role names match the starter roles in **Team → Roles** (`core/access.py`).

| Role | Who | Owns | Can't do |
|---|---|---|---|
| **Super Admin** | Founder | Sending: `enqueue` runs, turning Lob on or off for a campaign, browser calling (once built), campaign owners, granting Owner | — |
| **Owner** | Founder (and later a sales lead) | Campaign settings, cadence, templates, team and roles, weekly review, closing deals, invoices | Can't grant Owner or Super Admin |
| **Caller** | First hire, month 1 | Working the call list, logging every call, booking demos, marking do-not-call | Can't edit prospects or send email |
| **Sales Rep** | Hire, month 3 | Demos, proposals, follow-up after a demo, moving stages past `demo_scheduled`, renewal calls | Can't send campaigns or change cadence |
| **Template Editor** | Hire, month 3 (or the founder) | Email and mail templates and call scripts, A/B tests | Can't send |

Until those hires start, the founder does every role. Give each new person **only**
their role, by invite (`users invite --role Caller`), and add them as campaign
members so they see only the campaigns they work on.

## 2. Schedule

### Automatic (jobs)

These run on Railway only when `AGENCY_OS_RUN_JOBS=1` is set. Check
**/admin/jobs** for the last run and any errors.

| Job | Every | Does | Status |
|---|---|---|---|
| `pull-events` | 60 min | Pulls demo portal views, claims, publishes and `subscription.*` events from u9itus and moves stages forward | Built. Returns nothing until the u9itus branch is deployed. |
| `provision` | 24 h | Creates demo portals for new prospects | Built. Same dependency. |
| Calls-due list | — | Creates a call task a few days after a mailer is delivered | Planned (M2–M7, [MAILER_FOLLOWUP_CALLS.md](MAILER_FOLLOWUP_CALLS.md)) |
| Recording copy, transcription and retention | — | — | Planned ([BROWSER_CALLING.md](BROWSER_CALLING.md)) |

**Sending is never automatic.** Email, Lob and SMS go out only when a person runs
`enqueue`, because those sends cost money and reach real people.

### Daily (about 15 minutes, Owner)

1. Open **/admin/jobs**. If a job failed, fix it before doing anything else.
2. Check the email log for bounces and replies. Answer every reply the same day.
   Move anyone who replied to `engaged`.
3. Check the prospects marked "ready to close" (they claimed a portal), and hand
   each one to a caller or rep the same day.
4. Run the maintenance commands:

   ```bash
   python agency_os.py enrich --all --limit 50
   python agency_os.py stale --all
   ```

### Send day (twice a week, Super Admin)

1. Preview what's due with `enqueue --all --limit 50 --dry-run`, and read a few
   of the emails with `test-send` (it sends one to you).
2. Check the do-not-call and closed-lost prospects aren't in the batch.
3. Run `enqueue --all --limit 50` for real.
4. If Lob is on for a campaign, check the Lob dashboard balance first. Each piece
   costs about $0.62.

Keep batches small (50 or fewer per sender per day) until the domain's
reputation is established.

### Caller day (Caller)

| Block | Work |
|---|---|
| 9–10 am | Call back everyone who asked to be called today (the calendar's follow-ups) |
| 10 am–12 pm | `engaged` and `demo_scheduled` prospects, then `contacted` prospects who have a phone number |
| 1–3 pm | Cold calls to prospects whose email bounced or who have a phone but no email |
| 3–4 pm | Log anything missed, book demos, write up hand-offs to the rep |

Rules for every call:

- **Log every dial**, including no answer and voicemail, from the call log form.
  Pick the closest outcome; don't leave it blank.
- **Call only between 8 am and 9 pm in the prospect's local time.** In practice
  that means business hours.
- **If someone asks not to be called again**, log the outcome as *Asked not to be
  called again*. That marks the prospect do-not-call, moves the outreach to
  `closed_lost` and stops the cadence from texting them. Never call them again
  from any campaign.
- **Don't record calls on your own phone.** Recording comes with browser calling,
  which reads a disclosure first.
- **Pitch only what's built.** Until F1–F4 ship (PROPOSAL §4a), sell Starter as
  built. Don't promise multilingual guides, side-by-side comparison or
  coalition partner pages.

### Weekly (Monday, Owner, 45 minutes)

1. Run `digest --all` and read the pipeline across all campaigns.
2. Compare the numbers to the targets in section 5. Look at the step with the
   biggest drop-off first.
3. Review 3–5 call logs per caller with them: what was said, what the outcome
   was, and what to try next.
4. Check stale prospects (no touch in 14+ days at `engaged` or later).
5. Check open invoices and anyone past due (section 4).

### Monthly (Owner and Template Editor)

1. `sync --all` to refresh prospect lists.
2. Template review: open and reply rate per template, and retire the losers.
   Change one thing at a time.
3. Campaign review: cadence, channels and Lob spend against the deals each
   campaign produced.
4. Costs: Railway, Twilio, Lob and Smartlead invoices against the budget in
   PROPOSAL §11.
5. Access review: deactivate anyone who's left, and check who has Owner and
   `cli.use`.

## 3. Pipeline: who moves each stage

Stages only move forward (`core/tools.py`, `STAGES`). Whoever triggers a move
also has to do the next thing.

| Stage | Moved by | Next action | Who | Within |
|---|---|---|---|---|
| `cold` | Sync and enrich | First touch from the cadence | Super Admin (send day) | Next send day |
| `contacted` | The first send | Cadence follow-ups; a call if there's a phone number | Caller | Cadence timing |
| `engaged` | A reply, a portal view (`pull-events`) or a completed call | Book a demo | Caller | 1 business day |
| `demo_scheduled` | A booked demo (Calendly or logged by hand) | Run the demo; send a reminder the day before | Sales Rep (founder for now) | As booked |
| `proposal_sent` | The rep, after the demo | Follow up after 3 days and after 7 days | Sales Rep | 3 and 7 days |
| `closed_won` | Payment or a signed invoice; a portal claim marks "ready to close" | Onboarding (section 4) | Owner | 2 business days |
| `closed_lost` | Not interested, or asked not to be called | Record the reason in the notes; nothing else | Whoever logged it | — |
| `nurture` | `stale` | Leave until the renewal or next-cycle campaign | — | — |

**Hand-offs are written down.** When a caller books a demo, they add a note to
the prospect: the contact's name and role, what they care about, what was
promised and when the demo is.

## 4. After the sale

None of this is built in agency-os yet. It's a process until the pieces below
exist.

### Billing

- **Today:** payment gating P1–P3 and P5 is built on u9itus (branch
  `feat/portal-payment-gating`, not merged yet). Once it's deployed, an owner pays
  by Stripe Checkout from the builder's **Plan** panel before publishing, or staff
  record a check or invoice with `php artisan org:mark-paid`. **Don't start real
  outreach until it's deployed** (PROPOSAL §7, blocking work).
- **Interim, if a deal closes early:** the Owner sends a manual invoice, records
  the invoice number and amount in the prospect notes, and moves the prospect to
  `closed_won` only when it's paid or signed.
- **Past due:** remind at 7 days and at 14 days. At 30 days the Owner calls. Don't
  unpublish a live guide during an election without the Owner's decision.
- **Refunds:** the Owner decides each one and records it in the notes.

Paid prospect searches for u9itus customers
([U9ITUS_BILLING.md](U9ITUS_BILLING.md)) are billed on the u9itus side. agency-os
only reports what it delivered, so there is no invoicing for them here.

### Onboarding (within 2 business days of `closed_won`)

1. A welcome email from the Owner, with the login link and one thing to do first
   (upload a logo).
2. A 20-minute setup call: logo, embed code on their website, printable PDF and
   QR codes.
3. Check the guide is published and the embed works on their site.
4. Before ballots mail: a check-in to share the traffic dashboard and ask for a
   referral.

### Support

| Tier | Channel | Reply within |
|---|---|---|
| Starter | Email | 2 business days |
| Pro | Email and phone | 1 business day |
| Coalition | Named contact | Same business day |

In the four weeks before an election, reply to every tier the same day.

### Renewal

Renewal is worked like a sale (PROPOSAL §3, Step 4b):

- A renewal campaign whose prospects are `closed_won` accounts. `subscription.expired`
  events from u9itus enroll them once payment gating is live.
- The first touch is about 90 days before the next election's ballot-mailing date.
- Every renewal gets a call, because the contact may have changed since the last
  cycle.
- Local special elections are a reason to get in touch between statewide cycles.

## 5. Targets

Derived from the Phase 1 plan in PROPOSAL §7 (560 emails and 730 calls producing
10–25 responses, 5–10 demos and 2–5 deals in 60 days). These are starting
numbers; replace them with real rates after the first month.

| Measure | Weekly target | Where to see it |
|---|---|---|
| New prospects first touched | 70 email + 90 call | `digest`, email log |
| Calls logged (one caller) | 90–120 dials | Call log |
| Conversations (outcome *Completed*) | 10–15 | Call log |
| Replies and portal views | 2–3 | Email log, `pull-events` |
| Demos booked | 1 | Pipeline: `demo_scheduled` |
| Proposals sent | 1 every two weeks | Pipeline: `proposal_sent` |
| Deals closed | 2–5 in the first 60 days | Pipeline: `closed_won` |
| Bounce rate | Under 3% | Email log |

If the bounce rate goes over 5%, stop sending and clean the list before the next
send day.

## 6. Compliance

> These are working rules, not legal advice. Have counsel confirm them before the
> first real send, together with the recording disclosure (BROWSER_CALLING.md Q1).

| Area | Rule | Status |
|---|---|---|
| **Email (CAN-SPAM)** | Every commercial email needs a working way to opt out and the sender's postal address. An opt-out is honored within 10 business days and that person isn't emailed again. | **Built** (agency-os PR #19, `core/compliance.py`). Every outreach email gets the postal address and a signed unsubscribe link, and SMTP adds one-click `List-Unsubscribe` headers. Opt-outs go into `email_suppressions` and are never emailed again. Email channels don't send until `AGENCY_OS_POSTAL_ADDRESS` and `AGENCY_OS_UNSUBSCRIBE_SECRET` are set on Railway. |
| **Texts (TCPA)** | Text only numbers that are allowed to receive them; honor STOP. | Twilio handles STOP and HELP itself (`plugins/channels/sms_twilio.py`). The cadence never texts do-not-call prospects. |
| **Calls (TCPA)** | No calls before 8 am or after 9 pm in the recipient's time zone. Honor every request to stop calling. No prerecorded or AI voice. | Do-not-call tag built (commit `286b685`). Calling hours are a process rule; nothing enforces them. |
| **Do Not Call registry** | Most CBO numbers are business lines, but a director's mobile number may be on the national registry. | Process only. Counsel to confirm whether to scrub against the registry. |
| **Call recording** | A disclosure on every recorded call, in every state; California and several other states require every party's consent. | Planned with browser calling. No recording until then. |
| **Direct mail** | Lob postcards reach real addresses and cost money. | Only a Super Admin can turn Lob on for a campaign (`access.SUPER_ADMIN_CHANNELS`). |
| **Selling to 501(c)(3)s** | Don't suggest a guide can endorse candidates. Ballot-measure positions only. | u9itus enforces this per organization type. |
| **Access** | Least privilege; audit log on every change. | Built: roles, campaign members, audit log. |

## 7. What breaks and who fixes it

| Symptom | Likely cause | Who | First step |
|---|---|---|---|
| `pull-events` shows errors | u9itus API down or the token changed | Owner | Check `U9ITUS_BASE_URL` and `U9ITUS_AGENCY_TOKEN`; run `pull-events` by hand |
| Bounce rate over 5% | Bad enrichment or domain reputation | Owner | Stop sending; check SPF, DKIM and DMARC; remove the bounced addresses |
| A prospect says they were called after asking not to be | Do-not-call not logged | Owner | Mark do-not-call now; find the call that missed it and retrain |
| A caller can't see a campaign | Not a campaign member | Owner | **Campaign Settings → Who works this campaign** |
| The dashboard is down | Railway | Owner | Railway status page; redeploy |
| A new user can't do anything | No role yet (by design) | Owner | Give them a role in **Team** |

## 8. Open decisions

| # | Decision | Owner | Blocks |
|---|---|---|---|
| O1 | ~~Add the unsubscribe link and postal address~~ Built in the email channels (PR #19). Remaining: choose the postal address (a PO box or commercial mailbox is fine) and set it on Railway | Founder | First send |
| O2 | Scrub call lists against the national Do Not Call registry or not | Founder + counsel | Cold calling to mobile numbers |
| O3 | Caller pay: hourly, per booked demo, or both | Founder | First caller hire |
| O4 | Who owns renewals once there's a sales rep | Founder | Month 3 |
| O5 | Officeholder path or defer Phase 2 (PROPOSAL §7) | Founder | Any elected-official outreach |
