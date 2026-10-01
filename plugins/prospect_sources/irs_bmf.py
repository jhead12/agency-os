"""
IRS Exempt Organizations Business Master File (EO BMF) prospect source.

Downloads the California state CSV from irs.gov, filters to LA County
ZIPs and civic-engagement NTEE codes, yields Prospect objects.

Free public data — no API key needed.
"""

from __future__ import annotations

import csv
import io
from pathlib import Path
from typing import Iterator

import httpx

from core.models import Prospect


# NTEE code prefixes relevant to civic engagement / voter education
CIVIC_NTEE_PREFIXES = {"R", "W", "P", "S"}

# A subset of LA County ZIP prefixes (first 3 digits) for quick filtering.
# Full LA County ZIP list loaded from data/la_county_zips.txt if available.
LA_COUNTY_ZIP_PREFIXES = {
    "900", "901", "902", "903", "904", "905", "906", "907", "908",
    "910", "911", "912", "913", "914", "915", "916", "917", "918",
    "923", "924", "925", "928",
}


def _load_la_zips() -> set[str] | None:
    """Load full LA County ZIP set from data/la_county_zips.txt if it exists."""
    p = Path("data/la_county_zips.txt")
    if p.exists():
        return {line.strip() for line in p.read_text().splitlines() if line.strip()}
    return None


class IrsBmfSource:
    """IRS EO BMF — California nonprofits."""

    key = "irs_bmf"
    URL = "https://www.irs.gov/pub/irs-soi/eo_ca.csv"

    def is_configured(self) -> bool:
        return True  # free public data

    def discover(self, filters: dict) -> Iterator[Prospect]:
        state_filter = filters.get("state", "CA")
        county_filter = filters.get("county")
        ntee_filter = filters.get("ntee_codes")
        min_revenue = filters.get("min_revenue", 0)

        la_zips = _load_la_zips()

        # Stream the CSV
        with httpx.Client(timeout=120, follow_redirects=True) as client:
            resp = client.get(self.URL)
            resp.raise_for_status()

        reader = csv.DictReader(io.StringIO(resp.text))

        for row in reader:
            # State filter
            row_state = (row.get("STATE") or "").upper()
            if state_filter and row_state != state_filter.upper():
                continue

            # County filter (LA County by ZIP)
            zip_code = (row.get("ZIP") or "")[:5]
            if county_filter:
                if county_filter.lower() == "los angeles":
                    if la_zips:
                        if zip_code not in la_zips:
                            continue
                    elif zip_code[:3] not in LA_COUNTY_ZIP_PREFIXES:
                        continue
                else:
                    # For other counties, rely on city match or skip
                    continue

            # NTEE filter
            ntee = (row.get("NTEE_CD") or "").strip()
            ntee_prefix = ntee[:1] if ntee else ""
            if ntee_filter:
                if ntee_prefix not in ntee_filter:
                    continue
            elif ntee_prefix and ntee_prefix not in CIVIC_NTEE_PREFIXES:
                # Default: only civic-relevant categories
                continue

            # Revenue filter
            revenue = int(row.get("INCOME_AMT") or 0) or 0
            if min_revenue and revenue < min_revenue:
                continue

            name = (row.get("NAME") or "").strip().title()
            if not name:
                continue

            yield Prospect(
                name=name,
                ein=(row.get("EIN") or "").strip(),
                ntee_code=ntee,
                address=(row.get("STREET") or "").strip().title(),
                city=(row.get("CITY") or "").strip().title(),
                state=row_state,
                zip=zip_code,
                county=county_filter,
                annual_revenue=revenue or None,
                source=self.key,
                source_url=self.URL,
                focus_area=self._classify_focus(ntee_prefix),
                metadata={"ntee_full": ntee, "ruling_year": row.get("RULING")},
            )

    def _classify_focus(self, ntee_prefix: str) -> str:
        """Map NTEE prefix to a human-readable focus area."""
        mapping = {
            "R": "civil_rights_civic_engagement",
            "W": "public_society_benefit",
            "P": "human_services",
            "S": "community_improvement",
            "T": "philanthropy_volunteerism",
            "E": "environment",
            "H": "health",
            "L": "education",
        }
        return mapping.get(ntee_prefix, "other")