"""
Campaign configuration loader.

A campaign is a YAML file at campaigns/<name>/campaign.yaml that ties
together prospect sources, a product, channels, enrichers, filters,
pipeline stages, and a follow-up cadence. Script templates live as
YAML files alongside it in scripts/.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


@dataclass
class CadenceStep:
    touch: int
    delay_days: int
    script: str       # script file stem (e.g. "00_cold_outreach")
    next_stage: str   # stage to move to after this touch


@dataclass
class CampaignConfig:
    name: str
    product: str
    prospect_sources: list[str]
    channels: list[str]
    enrichers: list[str] = field(default_factory=list)
    filters: dict = field(default_factory=dict)
    stages: list[str] = field(default_factory=list)
    cadence: list[CadenceStep] = field(default_factory=list)
    stale_threshold_days: int = 14
    sender_name: str = ""
    sender_email: str = ""
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
            )
            for step in raw.get("cadence", [])
        ]

        return cls(
            name=raw["name"],
            product=raw["product"],
            prospect_sources=raw["prospect_sources"],
            channels=raw["channels"],
            enrichers=raw.get("enrichers", []),
            filters=raw.get("filters", {}),
            stages=raw.get("stages", []),
            cadence=cadence,
            stale_threshold_days=raw.get("stale_threshold_days", 14),
            sender_name=raw.get("sender_name", ""),
            sender_email=raw.get("sender_email", ""),
            config_dir=campaign_dir,
        )

    def load_script(self, script_stem: str) -> dict[str, Any]:
        """Load a script YAML from the campaign's scripts/ folder."""
        script_path = self.config_dir / "scripts" / f"{script_stem}.yaml"
        if not script_path.exists():
            raise FileNotFoundError(f"Script not found: {script_path}")
        return yaml.safe_load(script_path.read_text())

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