# Building plugins

A plugin adds something to agency-os by dropping files into `plugins/`. Nothing
needs registering and nothing in `core/` changes. This guide covers making a
whole plugin at once (a page, a card on prospect pages, where its data comes
from, a scheduled AI job and its own agent) and then each part on its own.

For the app's styles and components, open the UI kit at `/p/ui-kit`. It shows
each one live with its HTML to copy.

## Start with `new-plugin`

```bash
python agency_os.py new-plugin grant-finder --title "Grant finder"
python -m pytest tests/test_plugin_grant_finder.py
```

That writes a plugin whose parts already work together:

| File | What it is |
|---|---|
| `plugins/pages/grant_finder.py` | A page at `/p/grant-finder`: search prospects, read what the agent found, save a search as a list |
| `plugins/pages/templates/grant_finder.html` | The page's HTML |
| `plugins/pages/static/grant_finder.css` | Its styles |
| `plugins/panels/grant_finder.py` | A card on every prospect's page, linking to the page |
| `plugins/panels/templates/grant_finder.html` | What goes inside the card |
| `plugins/prospect_sources/grant_finder.py` | Prospects from a JSON feed, off until `GRANT_FINDER_SOURCE_URL` is set |
| `plugins/jobs/grant_finder.py` | A daily job: the agent reads each campaign's waiting prospects and picks who to work next |
| `plugins/agents/grant-finder.md` | The agent (persona), with a task of its own on every prospect page |
| `tests/test_plugin_grant_finder.py` | Checks that every part loads and the job works, without a database or AI model |

Restart the app and:

- the page is in the **More** menu for anyone with `prospects.view`;
- its card is on every prospect's page, where people can move or hide it;
- the job is on **Administration → Jobs**, where an Owner can run it now; it runs
  daily once `AGENCY_OS_RUN_JOBS=1`;
- the agent is in the **Ask an agent** panel on every prospect page and in the
  chat robot, for people who turned on AI features;
- the source starts finding prospects once you set its URL and add
  `- grant_finder` under `prospect_sources` in a campaign's `campaign.yaml`.

Then make it yours: change the agent's instructions, what the job asks it, what
the page searches and shows, and where the source reads from. The starter
templates live in `plugins/_starter/`. `new-plugin` never overwrites a file;
pick another name if one exists.

How the parts connect:

```
source ──sync──▶ prospects ──▶ page (search, save as a list ──▶ a lead package)
                    │                ▲
                    ▼                │ last_result()
               daily job ──ask──▶ agent

each prospect's page ──▶ the plugin's panel ──link──▶ page
```

## Pages

See [plugins/pages/README.md](../plugins/pages/README.md). A page class has a
`key`, `title`, `permission`, `template` and `context(page)`, plus an
optional `post(page, form)`. The app decides who can open it.

**Searching prospects.** Use `Database.prospect_filter(criteria, page.hidden_campaigns)`.
It's the same search as the Prospects page and saved lists (`q`, `source`,
`stage`, `campaign`, `cities`), and it leaves out campaigns the viewer may not
see. A search a page saves with `page.db.save_prospect_list(...)` shows up
under **My saved lists**, and an Owner can publish it as a lead package.

Set `nav = False` on a page to keep it out of the **More** menu (it still
opens at `/p/<key>` for people its `permission` allows). The UI kit does this.

## Panels

A card on the dashboard or on every prospect's page, without changing core
templates. See [plugins/panels/README.md](../plugins/panels/README.md). A panel
class has a `key`, `title`, `slot` (`"prospect"` or `"dashboard"`),
`permission`, `template` and `context(panel)`, plus an optional `width`,
`shown` and `about`. `context()` returning `None` leaves the card off.

Panels only read. Put forms on a plugin page and link to it from the card.
On the prospect page, `panel.prospect` is the prospect, already checked
against what the viewer may see. People arrange plugin cards with
**🎨 Customize** like the built-in ones.

## Scheduled jobs

A file in `plugins/jobs/` with one class:

```python
class StaleLeadsJob:
    key = "stale-leads"          # shown on /admin/jobs; not pull-events or provision
    label = "Rank stale leads"
    every_minutes = 24 * 60      # at least 5

    def run(self, job):
        reply = job.ask_agent("my-agent", "Which of these should we call first?", records)
        return {"campaigns": {...}}  # the run's summary, kept in job_runs
```

`job` (`core.jobs.JobContext`) has `db`, `registry`, `pipeline`, `campaigns`
(every active campaign), `ask_agent(persona, ask, data)` and `last_result()`
(the previous run, to pick up where it left off).

The rules:

- **Safe to repeat, and never sends or spends.** A job reads, drafts and
  records. Sending email, texts or mail and buying leads stay deliberate
  steps a person takes.
- **Jobs see every campaign, so results go per campaign.** Key the summary by
  `campaign.db_name`, and have the page show only the viewer's campaigns
  (the starter does this).
- **A summary with an `"error"` key marks the run failed.** An exception does
  too. Neither stops the app.
- Jobs only run on schedule with `AGENCY_OS_RUN_JOBS=1`.
  `AGENCY_OS_JOB_<KEY>_MINUTES` changes how often one runs (for example
  `AGENCY_OS_JOB_STALE_LEADS_MINUTES=360`). Jobs are found at startup, so
  restart to add one.
- A page reads a job's latest result with `core.jobs.last_result(db, key)`.

## Agents (personas)

A Markdown file in `plugins/agents/` (or `agents/`, for the vendored ones):

```markdown
---
name: Grant Scout
description: Finds which grants a prospect qualifies for.
emoji: 🔎
tasks:
  - call_prep
  - {key: grant_angle, label: Grant angle, ask: "Say which grant fits them and why."}
  - freeform
---

You are ... (the persona's instructions)
```

`tasks` lists what the agent offers on prospect pages. Use built-in keys
(`next_email`, `sms`, `call_prep`, `meddpicc`, `proposal_outline`, `freeform`)
or define your own task with `{key, label, ask}`. Without `tasks`, an agent offers every built-in
task. A task key must be lowercase and can't reuse a built-in key. Prefix
yours with the plugin's name so two plugins don't collide. If a file has the
same name as one in `agents/`, the `agents/` one wins.

Agents draft; they never send. Prospect records are given to the model as
data it must not take instructions from. That holds on prospect pages, in the
chat robot, over MCP and in scheduled jobs.

## Tutorials

A tutorial is a YAML file in `workflows/tutorials/`, played in the browser
for everyone its `requires` allows. A plugin can ship one to walk people
through its page: name it after the plugin (`NN-grant-finder.yaml`). The
format is in `core/workflows.py`, and the existing tutorials are good
examples. Tutorials are data, never code. A step that changes data asks the
person first, and the player skips steps that point at things not on the page.

## The other plugin types

| Folder | A class with | Turned on by |
|---|---|---|
| `prospect_sources/` | `key`, `is_configured()`, `discover(filters)` yielding `Prospect`s | a campaign's `prospect_sources` |
| `enrichers/` | `key`, `is_configured()`, `enrich(prospect)` | a campaign's `enrichers` |
| `channels/` | `key`, `is_configured()`, `send(...)` | a campaign's `channels` |
| `schedulers/` | `key`, `is_configured()`, `booking_link(...)`, `fetch_bookings(since)` | a campaign's `scheduler` |
| `products/` | `key`, `describe_value()`, `generate_demo_link()`, `pricing_tiers()` | a campaign's `product` |

The protocols are in `core/protocols.py`; copy a plugin in the same folder to
start. `python agency_os.py plugins` lists what's installed.

## What still needs a core change

A plugin can use only what already exists. A new permission
(`core/access.py`), a new table (`core/db.py`) or a new kind of plugin is a
core change and needs the owner's review (see `.github/CODEOWNERS`).
