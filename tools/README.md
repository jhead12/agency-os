# tools/ — agency-os toolkit

Convenience tools that sit next to the main CLI (`agency_os.py`). Each runs as
a module from the repo root and does one narrow job.

| Tool | Command | What it does |
|---|---|---|
| Doctor | `python -m tools.doctor` | Checks the environment: database reachable, which API keys/webhooks/AI vars are set. `--json` for machines. Exit 0 = core engine can run. |
| Inspect | `python -m tools.inspect <what>` | Answers "what do I have": `plugins`, `campaigns`, `tools`, `routes`, `stats`, or `all`. |
| Pipeline | `from tools.pipeline import setup, pick` | Importable engine setup (mirrors `core/cli._setup`) so scripts drive the pipeline without Click. |
| Wiki server | `python -m tools.wiki_serve --port 8888` | Serves `~/wiki/agency-os` (the knowledge wiki) as browsable HTML with `[[wikilinks]]` resolved. `--wiki` to point elsewhere. |

## Examples

```bash
python -m tools.doctor                          # what's missing from my env?
python -m tools.inspect plugins                 # which plugins are configured?
python -m tools.inspect campaigns               # cadences at a glance
python -m tools.inspect stats                   # per-campaign pipeline stats
python -m tools.wiki_serve --port 8888          # browse the wiki
```

```python
# a script that needs the pipeline
from tools.pipeline import setup, pipeline_for, pick

registry, db, campaigns = setup()
pipe = pipeline_for(db, registry)
pipe.check_stale(pick(campaigns, "voter-guide-cbo"))
```

The documentation wiki lives outside the repo at **`~/wiki/agency-os`** —
plain markdown, Obsidian-ready. Start at `index.md`, or serve it with
`tools.wiki_serve`.
