"""Inspect what agency-os has: plugins, campaigns, tools, routes, pipeline state.

    python -m tools.inspect plugins     # discovered plugins by type, with configured?
    python -m tools.inspect campaigns   # campaigns + product/channels/cadence summary
    python -m tools.inspect tools       # tool registry: name, kind, permission, surfaces
    python -m tools.inspect routes      # web routes from access.ROUTE_RULES
    python -m tools.inspect stats       # per-campaign pipeline stats from the DB
    python -m tools.inspect all
"""

from __future__ import annotations

import json
import sys

from tools.pipeline import setup

KIND_ICON = {"read": "·", "draft": "✎", "write": "●"}


def show_plugins(registry) -> None:
    from core import env  # noqa: F401  (ensures .env values are visible to is_configured)

    for label, mapping in [
        ("prospect_sources", registry.sources), ("products", registry.products),
        ("channels", registry.channels), ("enrichers", registry.enrichers),
        ("schedulers", registry.schedulers),
    ]:
        print(f"\n{label} ({len(mapping)}):")
        for key, plugin in sorted(mapping.items()):
            try:
                configured = bool(plugin.is_configured())
            except Exception:
                configured = False
            desc = getattr(plugin, "description", "") or ""
            print(f"  {'✓' if configured else '—'} {key:24s} {desc[:70]}")


def show_campaigns(campaigns: list) -> None:
    for c in campaigns:
        print(f"\n{c.db_name}  —  {c.name}")
        print(f"  product:   {c.product}")
        print(f"  sources:   {', '.join(c.prospect_sources)}")
        print(f"  enrichers: {', '.join(c.enrichers)}")
        print(f"  channels:  {', '.join(c.channels)}")
        if c.scheduler:
            print(f"  scheduler: {c.scheduler}")
        print("  cadence:   " + " → ".join(
            f"t{s.touch}:{s.script}(+{s.delay_days}d→{s.next_stage})" for s in c.cadence
        ))


def show_tools() -> None:
    from core import tools as toolr

    print(f"\n{'tool':22s} {'kind':6s} {'permission':18s} surfaces")
    for t in toolr.TOOLS.values():
        perm = t.permission if t.permission.startswith(("prospects", "calls", "campaigns", "agents", "pipeline")) else f"@{t.permission}" if not t.permission.startswith("@") else t.permission
        print(f"{t.name:22s} {KIND_ICON.get(t.kind, '?')} {t.kind:4s} {perm:18s} {','.join(t.surfaces)}")


def show_routes() -> None:
    from core import access

    print(f"\nROUTE_RULES ({len(access.ROUTE_RULES)}):")
    for rule in access.ROUTE_RULES:
        print(f"  {rule}")


def show_stats(campaigns: list, db) -> None:
    for c in campaigns:
        stats = db.get_pipeline_stats(c.db_name)
        print(f"\n{c.db_name}:")
        print(json.dumps(stats, indent=2, default=str))


def main(argv: list[str]) -> None:
    what = argv[1] if len(argv) > 1 else "all"
    registry, db, campaigns = setup()
    if what in ("plugins", "all"):
        show_plugins(registry)
    if what in ("campaigns", "all"):
        show_campaigns(campaigns)
    if what in ("tools", "all"):
        show_tools()
    if what in ("routes", "all"):
        show_routes()
    if what in ("stats", "all"):
        show_stats(campaigns, db)
    if what == "help":
        print(__doc__)


if __name__ == "__main__":
    main(sys.argv)
