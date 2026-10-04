"""
Campaign configuration loader.

A campaign is a YAML file at campaigns/<name>/campaign.yaml that ties
together prospect sources, a product, channels, enrichers, filters,
pipeline stages, and a follow-up cadence. Script templates live as
YAML files alongside it in scripts/.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml

if TYPE_CHECKING:
    from core.db import Database


@dataclass
class CadenceStep:
    touch: int
    delay_days: int
    script: str       # script file stem (e.g. "00_cold_outreach")
    next_stage: str   # stage to move to after this touch
    channels: list[str] = field(default_factory=list)  # overrides campaign channels for this touch


@dataclass
class CampaignConfig:
    name: str
    product: str
    prospect_sources: list[str]
    channels: list[str]
    enrichers: list[str] = field(default_factory=list)
    scheduler: str = ""  # scheduler plugin for {{booking_link}} + booking sync
    filters: dict = field(default_factory=dict)
    stages: list[str] = field(default_factory=list)
    cadence: list[CadenceStep] = field(default_factory=list)
    stale_threshold_days: int = 14
    sender_name: str = ""
    sender_email: str = ""
    lead_packages: dict = field(default_factory=dict)  # x402 spend policy, see core/payments.SpendPolicy
    config_dir: Path = None  # type: ignore

    @classmethod
    def load(cls, campaign_dir: str | Path) -> "CampaignConfig":
        campaign_dir = Path(campaign_dir)
        raw = yaml.safe_load((campaign_dir / "campaign.yaml").read_text())

        cadence = [
            CadenceStep(
                touch=step["touch"],
                delay_days=step["delay_days"],
                script=step["script"],
                next_stage=step["next_stage"],
                channels=step.get("channels", []),
            )
            for step in raw.get("cadence", [])
        ]

        return cls(
            name=raw["name"],
            product=raw["product"],
            prospect_sources=raw["prospect_sources"],
            channels=raw["channels"],
            enrichers=raw.get("enrichers", []),
            scheduler=raw.get("scheduler", ""),
            filters=raw.get("filters", {}),
            stages=raw.get("stages", []),
            cadence=cadence,
            stale_threshold_days=raw.get("stale_threshold_days", 14),
            sender_name=raw.get("sender_name", ""),
            sender_email=raw.get("sender_email", ""),
            lead_packages=raw.get("lead_packages") or {},
            config_dir=campaign_dir,
        )

    def load_script(self, script_stem: str) -> dict[str, Any]:
        """Load a script YAML from the campaign's scripts/ folder."""
        script_path = self.config_dir / "scripts" / f"{script_stem}.yaml"
        if not script_path.exists():
            raise FileNotFoundError(f"Script not found: {script_path}")
        return yaml.safe_load(script_path.read_text())

    @property
    def sequence_stages(self) -> list[str]:
        """Stages that still receive automated cadence touches."""
        stages = ["cold"]
        for step in self.cadence:
            if step.next_stage not in stages:
                stages.append(step.next_stage)
        return stages

    @property
    def db_name(self) -> str:
        """Slugified name for DB use."""
        import re
        return re.sub(r"[^a-z0-9-]", "", self.name.lower().replace(" ", "-"))


def discover_campaigns(base_dir: str | Path = "campaigns") -> list[CampaignConfig]:
    """Find all campaign directories with a campaign.yaml."""
    base = Path(base_dir)
    if not base.exists():
        return []
    campaigns = []
    for d in sorted(base.iterdir()):
        if d.is_dir() and (d / "campaign.yaml").exists():
            try:
                campaigns.append(CampaignConfig.load(d))
            except Exception as exc:
                print(f"  ! Failed to load campaign {d.name}: {exc}")
    return campaigns


def sync_campaign_files(db: "Database", seed_dir: str | Path, cache_dir: str | Path) -> Path:
    """Write the campaign files stored in the database out to cache_dir.

    Files under seed_dir (the repo's campaigns/) that the database doesn't have
    yet are added first, so new campaigns ship with a deploy while edits made
    in the dashboard are kept. cache_dir is rebuilt from scratch each time.
    """
    seed = Path(seed_dir)
    if seed.exists():
        db.seed_campaign_files({
            f.relative_to(seed).as_posix(): f.read_text()
            for f in sorted(seed.rglob("*.yaml"))
        })
    cache = Path(cache_dir)
    shutil.rmtree(cache, ignore_errors=True)
    for rel_path, content in db.campaign_files().items():
        target = cache / rel_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
    cache.mkdir(parents=True, exist_ok=True)
    return cache
