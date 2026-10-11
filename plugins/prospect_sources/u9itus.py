"""
u9itus source: businesses and organizations from u9itus's own directory.

Reads GET /api/v1/agency/prospects on u9itus with the agency token the portal
integration already uses (U9ITUS_BASE_URL, U9ITUS_AGENCY_TOKEN). Off until both
are set. The contract is in docs/U9ITUS_PROSPECT_SOURCE.md. u9itus leaves out
records someone has claimed and the demo portals agency-os made, so a customer
is never prospected and agency-os never prospects its own demos.

Filters in campaign.yaml:

    filters:
      u9itus_kinds: [business, organization]   # default: both
      state: CA
      cities: [Pasadena, Glendale]             # optional
      u9itus_categories: [restaurant, dentist] # businesses only, optional
      u9itus_org_types: [cbo, church]          # organizations only, optional
      u9itus_max: 2000                         # most records per kind per sync (default 2000)

Each record becomes a Prospect keyed by "<kind>:<u9itus id>"
(metadata.external_ref), with the record's contact seeded onto the outreach row.
"""

from __future__ import annotations

from typing import Iterator, Optional

from core.models import Prospect
from plugins.products.u9itus_client import U9itusClient

KINDS = ("business", "organization")
PAGE_SIZE = 200
DEFAULT_MAX = 2000
MAX_PAGES = 100  # a cursor that never ends can't loop forever


def _list(value) -> list[str]:
    if isinstance(value, str):
        value = value.split(",")
    return [str(v).strip() for v in (value or []) if str(v).strip()]


def _text(record: dict, key: str, limit: int = 300) -> Optional[str]:
    value = str(record.get(key) or "").strip()[:limit]
    return value or None


class U9itusSource:
    key = "u9itus"

    def __init__(self, client: Optional[U9itusClient] = None):
        self._client = client

    @property
    def client(self) -> U9itusClient:
        if self._client is None:
            self._client = U9itusClient()
        return self._client

    def is_configured(self) -> bool:
        return self.client.is_configured()

    def _prospect(self, kind: str, record: dict) -> Optional[Prospect]:
        name, rid = _text(record, "name"), _text(record, "id", 64)
        if not name or not rid:
            return None
        contact = record.get("contact") if isinstance(record.get("contact"), dict) else {}
        contact = {k: _text(contact, k) for k in ("name", "email", "phone", "title")}
        contact["phone"] = contact["phone"] or _text(record, "phone", 40)
        profile_url = _text(record, "profile_url", 500) or ""
        return Prospect(
            name=name,
            source=self.key,
            source_url=profile_url if profile_url.startswith("https://") else "",
            ein=_text(record, "ein", 20) if kind == "organization" else None,
            website_url=_text(record, "website_url", 500),
            address=_text(record, "street"),
            city=_text(record, "city"),
            state=(_text(record, "state", 2) or "").upper() or None,
            zip=_text(record, "zip", 10),
            focus_area=_text(record, "category" if kind == "business" else "org_type", 100),
            metadata={
                "external_ref": f"{kind}:{rid}",
                "u9itus": {"kind": kind, "id": rid, "slug": _text(record, "slug", 200), "profile_url": profile_url},
                **({"contact": contact} if any(contact.values()) else {}),
            },
        )

    def discover(self, filters: dict) -> Iterator[Prospect]:
        if not self.is_configured():
            return
        kinds = [k for k in _list(filters.get("u9itus_kinds")) or list(KINDS) if k in KINDS]
        most = int(filters.get("u9itus_max") or DEFAULT_MAX)
        for kind in kinds:
            params = {
                "kind": kind,
                "state": str(filters.get("state") or "").upper(),
                "cities": ",".join(_list(filters.get("cities"))),
                "categories": ",".join(_list(filters.get("u9itus_categories"))) if kind == "business" else "",
                "org_types": ",".join(_list(filters.get("u9itus_org_types"))) if kind == "organization" else "",
                "limit": PAGE_SIZE,
            }
            found, after = 0, None
            for _ in range(MAX_PAGES):
                page = self.client.prospects(**params, after=after)
                if page.get("error"):
                    raise ValueError(f"u9itus answered {page.get('status')}: {page.get('detail')}")
                for record in page.get("prospects") or []:
                    prospect = self._prospect(kind, record) if isinstance(record, dict) else None
                    if prospect is None:
                        continue
                    yield prospect
                    found += 1
                    if found >= most:
                        break
                after = page.get("next_after")
                if found >= most or not after:
                    break
