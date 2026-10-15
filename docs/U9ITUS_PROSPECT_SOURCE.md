# u9itus's directory as a prospect source

Spec for letting **agency-os** read businesses and organizations from
**u9itus** (`/Volumes/PRO-BLADE/Github/u9itus.dev`, Laravel) and run a hybrid
campaign on them: a Lob postcard, a call from a person once it lands, then
email, with AI drafting along the way. Written in the same format as
[U9ITUS_PORTAL_INTEGRATION.md](U9ITUS_PORTAL_INTEGRATION.md): the contract is
YAML, and tasks have file paths and acceptance checks.

Status (2026-10-15; candidates added, see "Candidates"):
- **agency-os side built** on `feat/u9itus-hybrid-campaign`: the source
  (`plugins/prospect_sources/u9itus.py`), `U9itusClient.prospects()`, the
  template campaign (`campaigns/u9itus-hybrid-local/`), the Call Coach agent
  (`plugins/agents/u9itus-call-coach.md`) and the mail→call cadence it runs on
  ([MAILER_FOLLOWUP_CALLS.md](MAILER_FOLLOWUP_CALLS.md) M2–M5). Tested against
  a fake client: `tests/test_u9itus_source.py`, `tests/test_call_tasks.py`.
- **u9itus side not started** (tasks P1–P3 below). Until it ships the source
  gets a 404 and the sync reports an error for it; nothing else is affected.

## Goal

u9itus already holds directories of local businesses and organizations. Those
whose page nobody has claimed are its best prospects. agency-os pulls them into
a campaign, mails each one a postcard, puts them on a caller's list once it
arrives, and follows up by email. This campaign is the model for other
customers: copy the campaign folder, point the source at their data, change the
copy.

## Constraints that shape the design

| Fact | Consequence |
|---|---|
| The portal integration already authenticates agency-os with a bearer token (`agency.token` middleware, `U9ITUS_AGENCY_TOKEN`) and agency-os pulls rather than receives | The new endpoint sits in the same `/api/v1/agency` group with the same token. No new secret, no webhook. |
| agency-os provisions demo portals as `organizations` rows with `source = 'agency_os'` | The endpoint **never** returns those, or agency-os would prospect its own demos. |
| Someone who claimed or owns a page is already a u9itus customer | Claimed and owned records are left out by default. |
| `organizations.claim_email` and `verification_email` are addresses people gave u9itus to verify a claim, not for marketing | The endpoint never returns them. `contact` holds only details the record publishes. |
| Callers are people (no AI dialing) and Lob mail costs money per piece | agency-os only drafts and queues. Mail and email go out on a deliberate `enqueue` run; calls come from the Calls due list. |
| u9itus customers can buy searches through `/api/v1/search-types` ([U9ITUS_BILLING.md](U9ITUS_BILLING.md)) | This is a **prospect source**, not a search plugin, so u9itus's directory is never offered back to its paying customers. |

## Contract

```yaml
endpoint: GET /api/v1/agency/prospects
auth: "Authorization: Bearer <U9ITUS_AGENCY_TOKEN>"     # existing agency.token middleware
rate_limit: existing throttle:60,1

query:
  kind: business | organization        # required
  state: CA                            # optional, two-letter
  cities: "Long Beach,Torrance"        # optional, comma-separated, case-insensitive exact match
  categories: "bakery,dentist"         # businesses only: businesses.category
  org_types: "cbo,church"              # organizations only: organizations.org_type
  after: "<cursor>"                    # opaque; from next_after
  limit: 200                           # default 100, max 200

always_excluded:
  organization:
    - source = 'agency_os'             # demo portals agency-os provisioned
    - is_active = false
    - user_id IS NOT NULL OR claim_verified_at IS NOT NULL   # claimed
  business:
    - owner_user_id IS NOT NULL OR verified_at IS NOT NULL   # claimed
  both:
    - records whose owner asked u9itus not to be contacted, if u9itus keeps such a flag

response_200:
  prospects:
    - id: 7                            # u9itus primary key; stable
      kind: business
      name: "Sunrise Bakery"
      slug: "sunrise-bakery"
      profile_url: "https://www.u9itus.com/…"   # the public page they'd claim; https only
      website_url: "https://sunrise.example"
      phone: "+16265550100"            # businesses.phone (public listing)
      street: "1 Colorado Blvd"        # businesses.street | organizations.street_address
      city: "Long Beach"               # businesses.city_name | organizations.city
      state: "CA"                      # businesses.state_abbr | organizations.state
      zip: "90802"                     # organizations.zip; businesses have none yet
      category: "bakery"               # businesses only
      org_type: "cbo"                  # organizations only
      ein: "11-1111111"                # organizations only, when known
      contact:                         # optional; only published details
        name: "Ana Ruiz"
        title: "Director"
        email: "info@sunrise.example"
        phone: "+16265550100"
  next_after: "7"                      # null on the last page

errors:                                # same shape as the other agency endpoints
  401: unauthorized
  422: {error: invalid, message: "kind must be business or organization"}
```

Order by `id` ascending and use the last id as the cursor (keyset paging), so
records added during a sync are neither skipped nor repeated.

## How agency-os uses it

`plugins/prospect_sources/u9itus.py` pages through each kind the campaign asks
for and turns each record into a prospect keyed by `"<kind>:<id>"`
(`metadata.external_ref`), so a re-sync updates rather than duplicates. The
record's `contact` (and a business's `phone`) fill the outreach row's empty
contact fields. A rep's edits are never overwritten. `profile_url` becomes the
prospect's `source_url`, which scripts use as `{{source_url}}`.

```yaml
# campaigns/<slug>/campaign.yaml
prospect_sources: [u9itus]
filters:
  u9itus_kinds: [business, organization]
  state: CA
  cities: [Long Beach, Torrance]
  u9itus_categories: [bakery]        # businesses only
  u9itus_org_types: [cbo, church]    # organizations only
  u9itus_max: 500                    # most records per kind per sync
```

## Candidates (kind: candidate)

Added 2026-10-15. Down-ballot candidates (city council, school board, county
and district seats) with little exposure already have a page on u9itus, built
from the registrar's candidate list. `campaigns/u9itus-candidates-local/` tells
them it's there and that claiming it is free. It is awareness, not a sales
sequence: the last touch says that if they want regular messaging to voters in
their area they can ask, and that request becomes the order.

A campaign gets candidates only when it names the kind; the default stays
`[business, organization]`.

```yaml
query (kind: candidate):
  state: CA
  counties: "San Diego County,Los Angeles County"   # optional, exact match
  cities: "Chula Vista"                              # optional
  levels: "city,county,school_board,local"           # optional; u9itus maps its governance_level values to these

always_excluded:
  candidate:
    - user_id IS NOT NULL OR claim_verified_at IS NOT NULL   # claimed page
    - is_running_candidate = false
    - candidates not on the current ballot (official_guide on_ballot = false)

response_200 (candidate):
  prospects:
    - id: 41
      kind: candidate
      name: "Lily Schworm"
      slug: "lily-schworm"
      profile_url: "https://www.u9itus.com/…"
      office: "Chula Vista Elementary School District, Trustee Area 2"
      level: school_board
      race: "<stable id of the contest>"             # every candidate in a race gets the same copy
      election_date: "2026-11-03"
      county: "San Diego County"
      city: "Chula Vista"                            # null for county-wide seats
      state: "CA"
      street: "…"                                    # only a published campaign mailing address
      zip: "…"
      website_url: "https://…"
      contact:                                       # only what the candidate published
        email: "…"                                   # e.g. the registrar's candidate list
        phone: "+1…"                                 # "Business" numbers only, never "Residence"
```

Contact rules on the u9itus side: never return `claim_email`,
`verification_email` or `receipt_email`, and never a number the registrar
labels as a residence. Most candidates have no published contact (in October
2026 only San Diego's list carried any), so most get the postcard or nothing.

Rules the campaign keeps:
- Same copy for every candidate in a race. Nothing mentions party, opponents
  or how anyone should vote (`tests/test_call_tasks.py` checks no script fills
  in a party).
- Calls are dialed by a person, to published business numbers only.
- Anyone who asks not to be contacted goes on the do-not-call list, which
  every campaign honors.

## Tasks (u9itus)

| ID | Task | Files | Acceptance check |
|---|---|---|---|
| P1 | `prospects` action: validate the query, apply the exclusions above, keyset page by id, map both models to the response shape | `app/Http/Controllers/Api/AgencyApiController.php` | Feature tests: an `agency_os` demo org, a claimed org and an owned business are never returned; `kind` is required; `next_after` is null on the last page; `claim_email` never appears in the body. |
| P2 | Route in the existing `/agency` group | `routes/api.php` (`Route::get('/prospects', …)->name('prospects')`) | Without the token: 401. With it: 200. |
| P3 | Copy this contract into u9itus's docs next to the portal contract | `doc/AGENCY_OS_INTEGRATION.md` | — |
| P4 | `kind: candidate` in the `prospects` action: unclaimed, running, on the current ballot; `levels`/`counties` filters; `race` key | `app/Http/Controllers/Api/AgencyApiController.php` | Feature tests: a claimed candidate is never returned; `claim_email`, `verification_email` and `receipt_email` never appear; `levels=school_board` returns only school board seats. |
| P5 | Keep the contact a candidate published on the registrar list (business phone, email), not residence numbers | the San Diego candidate-list import | A "Residence:" number is never stored; a "Business:" number and email are. |

## Setup (agency-os)

1. `U9ITUS_BASE_URL` and `U9ITUS_AGENCY_TOKEN` are already set for the portal
   integration. Nothing new.
2. Settle the offer and the copy in `campaigns/u9itus-hybrid-local/scripts/`
   (marked DRAFT), and the cities in its `campaign.yaml`.
3. Lob: set `LOB_API_KEY` and the `LOB_FROM_*` return address, and subscribe the
   Lob webhook to the delivered and returned events
   ([MAILER_FOLLOWUP_CALLS.md](MAILER_FOLLOWUP_CALLS.md) section 5). Without
   delivery scans every call step waits the 10-day fallback.
4. `agency-os sync --campaign <name>`, then a dry-run enqueue, then a real one.
   Callers work the list at **Outreach → Calls Due**.

## Making it the model for another customer

Copy `campaigns/u9itus-hybrid-local/`, then change `name`, `sender_*`,
`filters` and the script copy. If their data isn't in u9itus, write a source
like `plugins/prospect_sources/u9itus.py` against their API (or start from
`python agency_os.py new-plugin <name>`). The cadence, call tasks, Calls due
and the Call Coach agent need no changes.
