# Plugin panels

A plugin panel is a card a plugin adds to a core page: the **dashboard** or
**every prospect's page**. Like plugin pages, it lives entirely in this
folder, so adding one doesn't touch `web/app.py` or `core/`.

```
plugins/panels/
├── README.md            ← this file
├── open_grants.py       ← one class per panel
└── templates/
    └── open_grants.html ← what goes inside the card
```

## Adding a panel

1. Create `plugins/panels/open_grants.py`:

   ```python
   """Open grants: the grants a prospect could apply for."""


   class OpenGrantsPanel:
       key = "open-grants"            # unique among panels
       title = "Open grants"          # the card's heading
       slot = "prospect"              # "prospect" or "dashboard"
       permission = "prospects.view"  # who sees it
       template = "open_grants.html"  # in plugins/panels/templates/
       width = "third"                # optional: "third", "two-thirds" or "full"

       def context(self, panel):
           grants = find_grants(panel.prospect)  # your code
           if not grants:
               return None                       # nothing to show: no card
           return {"grants": grants}
   ```

2. Create `plugins/panels/templates/open_grants.html`. The app draws the card
   and its `<h3>` title, so the template is only what goes inside:

   ```html
   {% for g in grants %}
   <div class="detail-row"><span>{{ g.name }}</span><span>{{ g.amount|currency }}</span></div>
   {% endfor %}
   ```

3. Restart the app.

On the prospect page, the card joins the board, so people can move, resize,
color and hide it with **🎨 Customize** like the built-in cards. Set
`shown = False` to start it in the Customize library instead, and `about` to
describe it there. On the dashboard, plugin cards go in a row below the
campaigns.

For the classes to use inside the card, see the UI kit at `/p/ui-kit`.

## What `panel` gives you

| Field | What it is |
|---|---|
| `panel.user` | The signed-in user (`panel.user.can("calls.log")`, ...) |
| `panel.db` | The database (`core.db.Database`) |
| `panel.campaigns` | The campaigns this user may see |
| `panel.hidden_campaigns` | The names of campaigns this user may **not** see |
| `panel.prospect` | On the prospect page: the prospect (`.id`, `.name`, `.source`, ...). `None` on the dashboard |

Templates get what `context()` returned, plus `panel` (the panel object) and
`user`. The app's filters (`currency`, `fmt_date`, `days_ago`, `tel`) work.

## Rules

- **Who sees it** works like plugin pages: `permission` is a permission from
  `core/access.py`, `"@owner"` or `"@super_admin"`, and anything else hides the
  panel from everyone.
- **The prospect is already checked.** The app opens a prospect's page only for
  someone allowed to see that prospect. Any other prospects a panel queries
  must leave out `panel.hidden_campaigns`, as on plugin pages.
- **Panels only read.** For a form, link to a plugin page (`/p/<key>`) and
  handle the post there. Reloading a page should never change anything.
- **Keep it quick.** A panel runs on every page view. Read a scheduled job's
  result (`core.jobs.last_result`) rather than calling an AI model or a slow
  API here.
- **A broken panel is left off.** If `context()` or the template raises an
  error, the page shows without the panel and the app logs
  `! panels/<key>: ...`.
- **Keep `key` stable.** People's saved layouts refer to it as `plugin-<key>`.
- Name templates after the panel (`open_grants.html`). They load as
  `plugin-panel/<name>` and can't replace core templates.

`agency-os new-plugin <name>` makes a prospect-page panel together with a
page, source, job and agent. See [docs/PLUGINS.md](../../docs/PLUGINS.md).

## Where this lives in core (for reference)

- `core/plugin_panels.py`: discovery, access, rendering
- `core/panels.py`: the prospect page's board and saved layouts
- `web/app.py`: `render_plugin_panels()`, called by the dashboard and prospect routes
- `web/templates/dashboard.html` and `prospect_detail.html`: where the cards go
