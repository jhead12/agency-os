"""
Campaign templates: a whole campaign in one JSON file.

A template lives at campaign_templates/<key>/template.json (not campaigns/, so
it never runs as a house campaign; docs/U9ITUS_CAMPAIGN_ORDERS.md section 3):

    {
      "template": {"key", "label", "description", "status",
                   "params": [{"name", "label", "type", "required", "default", ...}],
                   "<map name>": {"<choice>": {"<field>": [...]}}},   # optional lookups
      "order":    {...},                       # catalog details, passed through as-is
      "campaign": {...},                       # same shape as campaign.yaml
      "scripts":  {"<script stem>": {...}}     # same shape as scripts/<stem>.yaml
    }

Two kinds of placeholder, so they never collide:

    [[name]]                filled once here, from the template's params
    [[name -> map.field]]   each chosen value of a list param looked up in
                            template.<map>[value].<field>, merged in order
    {{name}}                left alone; filled per prospect at send time
                            (core/pipeline.py _build_variables)

A placeholder that is a whole string keeps the param's type (a list stays a
list); one inside other text is replaced with its text form.
"""

from __future__ import annotations

import json
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from core.campaign import CampaignConfig

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "campaign_templates"
TEMPLATE_FILE = "template.json"
PARAM_TYPES = {"text", "textarea", "state", "email", "number", "list", "choice", "multi_choice"}
LIST_TYPES = {"list", "multi_choice"}
PLACEHOLDER = re.compile(r"\[\[\s*(\w+)(?:\s*->\s*(\w+)\.(\w+))?\s*\]\]")


class TemplateError(ValueError):
    """A template file is malformed, or the params given to it are invalid."""


def _as_list(value) -> list:
    if value is None:
        return []
    if isinstance(value, str):
        value = value.split(",")
    if not isinstance(value, list):
        value = [value]
    return [v.strip() if isinstance(v, str) else v for v in value if str(v).strip()]


@dataclass
class CampaignTemplate:
    key: str
    label: str
    description: str
    status: str
    params: list[dict] = field(default_factory=list)
    order: dict = field(default_factory=dict)
    raw: dict = field(default_factory=dict)
    path: Path = None  # type: ignore

    @classmethod
    def load(cls, path: str | Path) -> "CampaignTemplate":
        """Read a template.json (or the folder holding one) and check its shape."""
        path = Path(path)
        if path.is_dir():
            path = path / TEMPLATE_FILE
        try:
            raw = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            raise TemplateError(f"{path}: {exc}") from exc
        if not isinstance(raw, dict):
            raise TemplateError(f"{path}: top level must be an object")

        meta, campaign, scripts = raw.get("template"), raw.get("campaign"), raw.get("scripts")
        if not isinstance(meta, dict) or not str(meta.get("key") or "").strip():
            raise TemplateError(f"{path}: template.key is required")
        if not isinstance(campaign, dict):
            raise TemplateError(f"{path}: campaign must be an object")
        if not isinstance(scripts, dict):
            raise TemplateError(f"{path}: scripts must be an object")

        params = meta.get("params") or []
        names = set()
        for p in params:
            name = p.get("name") if isinstance(p, dict) else None
            if not name or not re.fullmatch(r"\w+", name):
                raise TemplateError(f"{path}: every param needs a name of letters, digits or _")
            if name in names:
                raise TemplateError(f"{path}: param {name} is listed twice")
            if p.get("type", "text") not in PARAM_TYPES:
                raise TemplateError(f"{path}: param {name} has unknown type {p.get('type')}")
            names.add(name)

        for step in campaign.get("cadence") or []:
            if step.get("script") not in scripts:
                raise TemplateError(f"{path}: cadence touch {step.get('touch')} uses script "
                                    f"{step.get('script')}, which is not in scripts")

        for match in PLACEHOLDER.finditer(json.dumps([campaign, scripts])):
            name, lookup = match.group(1), match.group(2)
            if name not in names:
                raise TemplateError(f"{path}: [[{name}]] is not one of the template's params")
            if lookup and not isinstance(meta.get(lookup), dict):
                raise TemplateError(f"{path}: {match.group(0)} needs template.{lookup}")

        return cls(key=meta["key"].strip(), label=str(meta.get("label") or meta["key"]),
                   description=str(meta.get("description") or ""), status=str(meta.get("status") or "draft"),
                   params=params, order=raw.get("order") or {}, raw=raw, path=path)

    # ── Params ─────────────────────────────────────────────────────────

    def resolve_params(self, given: dict | None = None) -> dict[str, Any]:
        """The params with defaults applied, each checked against its field."""
        given = given or {}
        unknown = set(given) - {p["name"] for p in self.params}
        if unknown:
            raise TemplateError(f"unknown params: {', '.join(sorted(unknown))}")
        return {p["name"]: self._check(p, given.get(p["name"], p.get("default"))) for p in self.params}

    @staticmethod
    def _check(p: dict, value):
        name, kind = p["name"], p.get("type", "text")
        empty = value is None or value == "" or (kind in LIST_TYPES and not _as_list(value))
        if empty:
            if p.get("required"):
                raise TemplateError(f"{name} is required")
            return [] if kind in LIST_TYPES else None

        if kind == "number":
            try:
                value = int(value)
            except (TypeError, ValueError):
                raise TemplateError(f"{name} must be a whole number") from None
            if "max" in p and value > p["max"]:
                raise TemplateError(f"{name} can be at most {p['max']}")
            if value < int(p.get("min", 0)):
                raise TemplateError(f"{name} can be no less than {p.get('min', 0)}")
            return value

        values = _as_list(value) if kind in LIST_TYPES else [value]
        for v in values:
            if not isinstance(v, str):
                raise TemplateError(f"{name} must be text")
            # Param text must not smuggle in placeholders of either kind.
            if "{{" in v or "[[" in v:
                raise TemplateError(f"{name} may not contain {{{{ or [[")
            if "max" in p and len(v) > p["max"]:
                raise TemplateError(f"{name} can be at most {p['max']} characters")
        if kind in {"choice", "multi_choice"}:
            bad = [v for v in values if v not in (p.get("choices") or [])]
            if bad:
                raise TemplateError(f"{name}: {', '.join(bad)} is not one of {', '.join(p.get('choices') or [])}")
        if kind == "state":
            if not re.fullmatch(r"[A-Za-z]{2}", values[0]):
                raise TemplateError(f"{name} must be a two-letter state")
            return values[0].upper()
        if kind == "email" and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", values[0]):
            raise TemplateError(f"{name} must be an email address")
        return values if kind in LIST_TYPES else values[0].strip()

    # ── Rendering ──────────────────────────────────────────────────────

    def _value(self, params: dict, name: str, lookup: str | None, fld: str | None):
        if not lookup:
            return params[name]
        table, merged = self.raw["template"][lookup], []
        for choice in _as_list(params[name]):
            for v in _as_list((table.get(choice) or {}).get(fld)):
                if v not in merged:
                    merged.append(v)
        return merged

    def _fill(self, node, params: dict):
        if isinstance(node, dict):
            return {k: self._fill(v, params) for k, v in node.items()}
        if isinstance(node, list):
            return [self._fill(v, params) for v in node]
        if not isinstance(node, str):
            return node
        whole = PLACEHOLDER.fullmatch(node.strip())
        if whole:
            return self._value(params, *whole.groups())

        def text(m):
            v = self._value(params, *m.groups())
            return ", ".join(map(str, v)) if isinstance(v, list) else "" if v is None else str(v)
        return PLACEHOLDER.sub(text, node)

    def render(self, params: dict | None = None) -> tuple[str, dict[str, str]]:
        """Fill the template; returns (campaign slug, {relative path: YAML text}).

        Paths are "<slug>/campaign.yaml" and "<slug>/scripts/<stem>.yaml", the
        layout of campaigns/ and of the campaign_files table. The result is
        loaded with CampaignConfig before it is returned, so a template that
        renders a campaign the app can't load fails here, not at send time.
        """
        values = self.resolve_params(params)
        campaign = self._fill(self.raw["campaign"], values)
        scripts = {stem: self._fill(s, values) for stem, s in self.raw["scripts"].items()}

        dump = lambda data: yaml.safe_dump(data, sort_keys=False, allow_unicode=True, width=1000)
        header = f"# Made from campaign template {self.key} ({self.status}).\n"
        body = {"campaign.yaml": header + dump(campaign)}
        body.update({f"scripts/{stem}.yaml": dump(s) for stem, s in scripts.items()})

        with tempfile.TemporaryDirectory() as tmp:
            for rel, content in body.items():
                (Path(tmp) / rel).parent.mkdir(parents=True, exist_ok=True)
                (Path(tmp) / rel).write_text(content)
            try:
                config = CampaignConfig.load(tmp)
                for step in config.cadence:
                    config.load_script(step.script)
            except Exception as exc:
                raise TemplateError(f"template {self.key} renders a campaign that won't load: {exc}") from exc
        slug = config.db_name
        if not slug:
            raise TemplateError("the campaign name must contain letters or digits")
        return slug, {f"{slug}/{rel}": content for rel, content in body.items()}


def rendered_channels(slug: str, files: dict[str, str]) -> list[str]:
    """The channels of a campaign render() made."""
    return list((yaml.safe_load(files[f"{slug}/campaign.yaml"]) or {}).get("channels") or [])


def compose_receipt(template: CampaignTemplate, slug: str, values: dict, channels: list[str],
                    name: str, site_url: str) -> tuple[str, str]:
    """Return (subject, body) for the email to whoever made a campaign from a template."""
    settings = "\n".join(f"- {k}: {', '.join(map(str, v)) if isinstance(v, list) else v}"
                         for k, v in values.items() if v not in (None, "", []))
    body = f"""Hi {name},

You made the campaign {slug} from the "{template.label}" template.

Channels: {", ".join(channels) or "none"}

Settings:
{settings or "- (defaults)"}

Nothing has been sent yet. Review its copy before the first send:
{site_url}/admin/campaigns/{slug}
"""
    if "lob_direct_mail" in channels:
        body += "\nIt includes Lob direct mail, which costs money per piece once the cadence runs.\n"
    return f"Campaign made: {slug}", body


def discover_templates(base_dir: str | Path = TEMPLATES_DIR) -> list[CampaignTemplate]:
    """Every campaign_templates/<key>/template.json that loads; a bad one is skipped with a log line."""
    base = Path(base_dir)
    if not base.exists():
        return []
    found = []
    for d in sorted(base.iterdir()):
        if d.is_dir() and (d / TEMPLATE_FILE).exists():
            try:
                found.append(CampaignTemplate.load(d))
            except TemplateError as exc:
                print(f"  ! Failed to load campaign template {d.name}: {exc}")
    return found


def get_template(key: str, base_dir: str | Path = TEMPLATES_DIR) -> CampaignTemplate:
    for t in discover_templates(base_dir):
        if t.key == key:
            return t
    raise TemplateError(f"no campaign template {key}")


def create_campaign(db, template: CampaignTemplate, params: dict | None = None,
                    taken: list[str] | tuple = ()) -> str:
    """Make a campaign from a template and store its files in the database.

    The campaign appears on the next campaign sync (sync_campaign_files), like
    one made at /admin/campaigns/create. Refuses a slug that is taken: a stored
    campaign folder, or one of `taken` (the db_names of the loaded campaigns).
    """
    slug, files = template.render(params)
    if slug in taken or any(path.split("/", 1)[0] == slug for path in db.campaign_files()):
        raise TemplateError(f"campaign {slug} already exists")
    for path, content in files.items():
        db.save_campaign_file(path, content)
    return slug
