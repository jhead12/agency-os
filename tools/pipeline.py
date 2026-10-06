"""Importable pipeline helpers — the engine from core/pipeline.py without Click.

Used by toolkit scripts and anything else that wants to drive agency-os
programmatically instead of shelling out to agency_os.py.

    from tools.pipeline import setup, campaigns
    db, registry, pipe = setup()
    pipe.sync_prospects(campaigns("voter-guide-cbo"), dry_run=True)
"""

from __future__ import annotations

import os
import sys

# Allow `python -m tools.pipeline` from anywhere
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from core.env import load_dotenv  # noqa: E402

load_dotenv()

from core.campaign import discover_campaigns, sync_campaign_files  # noqa: E402
from core.db import Database  # noqa: E402
from core.pipeline import Pipeline  # noqa: E402
from core.registry import PluginRegistry  # noqa: E402

import tempfile  # noqa: E402
from pathlib import Path  # noqa: E402


def setup(campaigns_dir: str = "campaigns", plugins_dir: str = "plugins",
          db_url: str = "") -> tuple[PluginRegistry, Database, list]:
    """Build (registry, db, campaigns) exactly the way core/cli._setup does.

    Campaign files are synced into the DB first so the toolkit sees the same
    campaign config the dashboard edits.
    """
    registry = PluginRegistry()
    registry.discover(plugins_dir)
    db = Database(db_url or None)
    cache_dir = os.environ.get(
        "AGENCY_OS_CAMPAIGNS_DIR",
        str(Path(tempfile.gettempdir()) / "agency-os-campaigns"),
    )
    campaign_list = discover_campaigns(sync_campaign_files(db, campaigns_dir, cache_dir))
    return registry, db, campaign_list


def pipeline_for(db: Database, registry: PluginRegistry) -> Pipeline:
    return Pipeline(db, registry)


def pick(campaign_list, name: str):
    """Resolve one campaign by exact db_name, display name, or unique word-match
    (same rules as core/cli._get_campaign)."""
    name_slug = name.lower().replace(" ", "-").replace("_", "-")
    for c in campaign_list:
        if c.db_name == name_slug or c.name.lower() == name.lower():
            return c
    words = [w for w in name_slug.split("-") if w]
    matches = [c for c in campaign_list if words and all(w in c.db_name.split("-") for w in words)]
    if len(matches) == 1:
        return matches[0]
    raise SystemExit(
        f"Campaign '{name}' not found or ambiguous. "
        f"Have: {', '.join(c.db_name for c in campaign_list)}"
    )
