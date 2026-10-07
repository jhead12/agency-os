# Plugin page templates

The HTML for plugin pages goes here (Jinja templates, like
`web/templates/`). See `../README.md` for how pages work.

## Starter template

```html
{% extends "base.html" %}
{% block title %}{{ plugin_page.title }} — agency-os{% endblock %}
{% block content %}

<div class="page-header">
    <div>
        <h1>{{ plugin_page.title }}</h1>
        <p class="subtitle">One line about what this page is for</p>
    </div>
</div>

{% if msg %}<div class="save-notice">{{ msg }}</div>{% endif %}
{% if error %}<div class="form-error">{{ error }}</div>{% endif %}

<!-- your content -->

{% endblock %}
```

Extending `base.html` gives the page the app's nav, styles (`/static/style.css`) and the
mobile menu. Copying class names from a similar
core page in `web/templates/` (such as `plugins.html` or `dashboard.html`)
makes yours match the rest of the app.

## What every template gets

| Name | What it is |
|---|---|
| everything `context()` returned | your own values |
| `plugin_page` | the page object: `plugin_page.key`, `plugin_page.title` |
| `can_post` | whether this user may submit the page's form |
| `msg`, `error` | the message from the last form post, if there was one |
| `request` | the request. `request.state.user` is the signed-in user |

The app's filters work here too:

| Filter | Example | Output |
|---|---|---|
| `currency` | `{{ 125000 \| currency }}` | `$125K` |
| `fmt_date` | `{{ row.created_at \| fmt_date }}` | `Oct 06, 2026` |
| `days_ago` | `{{ row.created_at \| days_ago }}` | `3 days ago` |
| `days_until` | `{{ row.due \| days_until }}` | `5` |
| `tel` | `<a href="{{ phone \| tel }}">` | `tel:+15551234567` |

## Rules

- **Name files after the page** (`team_stats.html`, not `list.html`). Several
  pages share this folder.
- **Your templates can't replace core ones.** They load as `plugin/<name>`, so
  a `base.html` in this folder is just another plugin template. It does **not**
  change the app's layout. To change core look and feel, edit
  `web/templates/` or `web/static/` (also yours under CODEOWNERS).
- You can split pieces into partials and pull them in by their full name:
  `{% include "plugin/_team_stats_row.html" %}`.
- Jinja escapes values automatically. Don't use `| safe` on anything a user
  typed.
- Templates reload on refresh. Only a new or renamed **page class** needs a
  restart.
