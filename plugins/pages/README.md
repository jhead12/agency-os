# Plugin pages

A plugin page is a page in the web app that lives entirely under `plugins/pages/`.
You can add one without touching `web/app.py`, `core/` or anything else that
needs the owner's approval. Everything in this folder is yours to change
(see `.github/CODEOWNERS`).

```
plugins/pages/
├── README.md            ← this file
├── my_page.py           ← one class per page
├── templates/           ← the page's HTML (Jinja), see templates/README.md
│   └── my_page.html
└── static/              ← CSS, JS and images, served at /plugin-static/
    └── my_page.css
```

## Adding a page

To start a page together with a prospect source, a scheduled AI job and an
agent, run `python agency_os.py new-plugin <name>` (see
[docs/PLUGINS.md](../../docs/PLUGINS.md)). To add a card to the dashboard or prospect pages instead, see
[plugins/panels/README.md](../panels/README.md). For the app's styles and
components, open the UI kit at `/p/ui-kit` (`ui_kit.py` in this folder). To add just a page:

1. Create `plugins/pages/team_stats.py`:

   ```python
   """Team stats: calls and stages per campaign."""


   class TeamStatsPage:
       key = "team-stats"            # the page's address: /p/team-stats
       title = "Team stats"          # the label in the nav's "More" menu
       permission = "calls.view"     # who may open it (see "Who can see a page")
       template = "team_stats.html"  # in plugins/pages/templates/

       def context(self, page):
           """Return the values the template uses."""
           return {
               "rows": [
                   {"campaign": c.name, "stats": page.db.get_pipeline_stats(c.db_name)}
                   for c in page.campaigns
               ],
           }
   ```

2. Create `plugins/pages/templates/team_stats.html`:

   ```html
   {% extends "base.html" %}
   {% block title %}{{ plugin_page.title }} — agency-os{% endblock %}
   {% block content %}
   <div class="page-header"><div><h1>{{ plugin_page.title }}</h1></div></div>
   {% for row in rows %}
     <p>{{ row.campaign }}: {{ row.stats.total_prospects }} prospects</p>
   {% endfor %}
   {% endblock %}
   ```

3. Restart the app. The page appears at `/p/team-stats` and in the **More** menu
   for everyone who has `calls.view`.

That's it. Nothing else needs registering. Pages are found when the app
starts, the same way as the other plugins, so a new or renamed page needs a
restart. Editing a template only needs a browser refresh.

## The page class

| Attribute / method | Required | What it does |
|---|---|---|
| `key` | yes | The URL: `/p/<key>`. Use lowercase letters, digits, `-` or `_`, up to 64 characters. |
| `title` | yes | The label in the nav, also available to the template as `plugin_page.title`. |
| `permission` | yes | Who may open the page. |
| `template` | yes | The template's file name in `templates/`. |
| `context(page)` | yes | Returns a dict of values for the template. |
| `post(page, form)` | no | Handles a form posted to `/p/<key>` (see "Forms"). |
| `post_permission` | no | Who may post. Without it, `permission` applies. |
| `nav` | no | `False` keeps the page out of the **More** menu. It still opens at `/p/<key>`. |

A class that is missing a required attribute is skipped at startup, and the
app prints `! pages/<key>: needs …` in its log.

### What `page` gives you

The `page` argument (`core.plugin_pages.PageContext`) describes the current request:

| Field | What it is |
|---|---|
| `page.user` | The signed-in user. `page.user.name`, `page.user.email`, `page.user.can("calls.log")`, `page.user.is_owner` |
| `page.db` | The database (`core.db.Database`) |
| `page.query` | The URL's query string as a dict: `/p/team-stats?campaign=x` → `{"campaign": "x"}` |
| `page.campaigns` | The campaigns this user may see |
| `page.hidden_campaigns` | The names of campaigns this user may **not** see |

`context()` and `post()` are ordinary (non-async) functions. They run on a
worker thread, so slow queries don't block other requests.

## Who can see a page

The app decides access, not the page:

- `permission` must be one of the permissions in `core/access.py` (`CATALOG`),
  such as `"dashboard.view"`, `"calls.view"` or `"templates.edit"`. It can also be
  `"@owner"` (owners only) or `"@super_admin"`.
- Anything else denies **everyone**, owners included. That covers a typo
  (`"calls.veiw"`), `"@public"` and `"@user"`. This is on purpose: a mistake
  hides the page instead of opening it up.
- Signed-out visitors are sent to the login page first.
- The **More** menu only lists pages the user can open. It doesn't show up at
  all if they can't open any.
- AI agent accounts follow the same permission rules as everywhere else.

A page can only use permissions that already exist. A new permission (say
`"reports.view"`) is a change to `core/access.py` and needs the owner's approval.
Ask for it once; after that, every page can use it.

### Prospect data: respect hidden campaigns

Some campaigns are restricted (for example, recruiting leads only recruiters
may see). **If your page reads prospects, filter out the hidden campaigns.**
The app does this for its own routes, but for queries a page writes itself,
the page has to do it:

```python
def context(self, page):
    hidden_sql, params = page.db.hidden_clause(page.hidden_campaigns)
    rows = page.db.conn.execute(
        f"SELECT p.id, p.name FROM prospects p WHERE {hidden_sql} ORDER BY p.name LIMIT 50",
        params,
    ).fetchall()
    return {"prospects": rows}
```

For anything per campaign, loop over `page.campaigns`, which already leaves
out the hidden ones.

## Forms

Add a `post` method, and point a form at the page's own address:

```python
class NotesPage:
    key = "notes"
    title = "Notes"
    permission = "dashboard.view"      # who may read the page
    post_permission = "prospects.edit"  # who may submit the form
    template = "notes.html"

    def context(self, page):
        return {"notes": ...}

    def post(self, page, form):
        text = form.get("text", "").strip()
        if not text:
            raise ValueError("Write something first.")  # shown as an error
        ...  # save it
        return "Note saved."                             # shown as a success message
```

```html
{% if can_post %}
<form method="post" action="/p/{{ plugin_page.key }}">
  <textarea name="text"></textarea>
  <button type="submit">Save</button>
</form>
{% endif %}
```

- `form` is a dict of the submitted text fields. File uploads aren't passed through.
- After a post, the browser is sent back to `/p/<key>` with your message
  (`msg`) or the `ValueError` text (`error`). Show them in the template (see
  `templates/README.md`).
- Forms posted from another site are refused automatically. You don't need a
  CSRF token.
- To use one form for several actions, give the submit buttons a `name`
  (`<button name="action" value="delete">`) and check `form["action"]`.
- To send a file instead of a message (a CSV export, say), return
  `core.plugin_pages.Download("export.csv", text)`. The browser downloads it and
  stays on the page. `media_type` defaults to `text/csv`.
- To keep an audit trail, call `page.db.audit(page.user, "<what happened>", "<kind>", <id>)`.

## Static files

Files in `plugins/pages/static/` are served at `/plugin-static/<file>`:

```html
{% block content %}
<link rel="stylesheet" href="/plugin-static/team_stats.css">
...
<script src="/plugin-static/team_stats.js" defer></script>
{% endblock %}
```

These files are **public**, like `/static`. Anyone can download them without
signing in, so never put keys, customer data or anything private here.

## Updating pages safely

A page's behavior is defined entirely by the files in this folder, so
redeploying the same files always gives the same page:

- **Keep `key` stable.** It is the page's URL and the nav link. Changing it
  breaks bookmarks.
- **Prefix file names with the page.** Use `team_stats.html` and
  `team_stats.css`, not `table.html`, so two pages never collide.
- **Don't create tables or change data on page load.** Read in `context()`
  and write only in `post()`. Reloading a page should never change anything.
- **If a page needs new data, a new table or a new permission,** that's a core
  change (`core/db.py` or `core/access.py`) and needs the owner's review.

## Tests

`tests/test_plugin_pages.py` covers the framework itself: access, the nav,
forms and cross-site posts. Add tests for your own page under `tests/`. That
folder is also yours. The fixtures in that file show how to sign in as a user
with a given role and request a page:

```python
from tests.test_access import client_for, db, make_user

def test_team_stats_page(db):
    make_user(db, "caller@x.com", "Caller")
    r = client_for("caller@x.com").get("/p/team-stats")
    assert r.status_code == 200
```

Run them with a test database:

```
TEST_DATABASE_URL=postgresql://localhost/agency_os_test python -m pytest tests/
```

## Where this lives in core (for reference)

You don't need to change these, but they're useful to read:

- `core/plugin_pages.py`: discovery, the access rules, `PageContext`
- `web/app.py`, "Plugin pages" section: the `/p/{page_key}` routes
- `core/access.py`: the permission list (`CATALOG`)
- `web/templates/base.html`: the **More** menu
