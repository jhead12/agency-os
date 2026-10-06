"""
NPI Registry prospect source: licensed healthcare practices (dentists,
chiropractors, ...) from the CMS National Plan & Provider Enumeration System.

Free federal public data, no API key: https://npiregistry.cms.hhs.gov/api-page

Campaign filters (campaign.yaml `filters`):

    state: CA
    npi_taxonomies: [Dentist, Chiropractor]   # NPI taxonomy descriptions
    cities: [Pasadena, Glendale]              # and/or postal_codes: ["91101", ...]
    npi_type: organization                    # organization (practices, default) | individual

The API returns at most 1,200 records per search (200 a page, skip up to
1,000), so a statewide pull is split by city or ZIP; a search that hits the
cap is reported so the list can be narrowed.

Each practice becomes a Prospect keyed by its NPI number (metadata.external_ref)
with the practice phone and its authorized official (owner/manager) as the
contact. Sole proprietors' listed address can be a home, so they are flagged
(metadata.possible_home_address) for whoever sells the list.
"""

from __future__ import annotations

import re
import time
from typing import Iterator, Optional

import httpx

from core.models import Prospect

API_URL = "https://npiregistry.cms.hhs.gov/api/"
PAGE = 200
MAX_SKIP = 1000
NPI_TYPES = {"organization": "NPI-2", "individual": "NPI-1"}


def _phone(raw: Optional[str]) -> Optional[str]:
    digits = "".join(ch for ch in str(raw or "") if ch.isdigit())
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    return f"({digits[:3]}) {digits[3:6]}-{digits[6:]}" if len(digits) == 10 else None


def _title(text: Optional[str]) -> str:
    return " ".join(w.capitalize() for w in str(text or "").split())


def _location(result: dict) -> dict:
    addresses = result.get("addresses") or []
    return next((a for a in addresses if a.get("address_purpose") == "LOCATION"), addresses[0] if addresses else {})


def to_prospect(result: dict, wanted: set[str], source_key: str, place: tuple = (None, None)) -> Optional[Prospect]:
    """A Prospect for one active NPI record whose taxonomies include one we want (by
    prefix: "Dentist" covers "Dentist, General Practice") and whose practice location
    is the searched city or ZIP (the API also matches mailing addresses), else None."""
    basic = result.get("basic") or {}
    number = str(result.get("number") or "")
    if not number or basic.get("status", "A") != "A":
        return None
    taxonomies = result.get("taxonomies") or []
    matched = [t for t in taxonomies if str(t.get("desc", "")).lower().startswith(tuple(wanted))]
    if not matched:
        return None
    taxonomy = next((t for t in matched if t.get("primary")), matched[0])
    loc = _location(result)
    place_field, place_value = place
    if place_field == "city" and str(loc.get("city", "")).lower() != str(place_value).lower():
        return None
    if place_field == "postal_code" and not str(loc.get("postal_code", "")).startswith(str(place_value)):
        return None
    phone = _phone(loc.get("telephone_number"))

    if result.get("enumeration_type") == "NPI-2":
        name = _title(basic.get("organization_name"))
        contact = {
            "name": _title(f"{basic.get('authorized_official_first_name', '')} "
                           f"{basic.get('authorized_official_last_name', '')}") or None,
            "title": _title(basic.get("authorized_official_title_or_position")) or None,
            "phone": phone or _phone(basic.get("authorized_official_telephone_number")),
        }
        sole_proprietor = False
    else:
        person = _title(f"{basic.get('first_name', '')} {basic.get('last_name', '')}")
        credential = str(basic.get("credential") or "").strip()
        name = f"{person}, {credential}" if credential else person
        contact = {"name": person or None, "title": taxonomy.get("desc"), "phone": phone}
        sole_proprietor = basic.get("sole_proprietor") == "YES"
    if not name:
        return None

    return Prospect(
        name=name,
        source=source_key,
        source_url=f"https://npiregistry.cms.hhs.gov/provider-view/{number}",
        address=_title(" ".join(filter(None, [loc.get("address_1"), loc.get("address_2")]))) or None,
        city=_title(loc.get("city")) or None,
        state=loc.get("state") or None,
        zip=str(loc.get("postal_code") or "")[:5] or None,
        focus_area=re.sub(r"[^a-z0-9]+", "_", str(taxonomy.get("desc", "")).lower()).strip("_") or None,
        metadata={
            "external_ref": number,
            "npi_type": result.get("enumeration_type"),
            "taxonomy_code": taxonomy.get("code"),
            "license": taxonomy.get("license"),
            "possible_home_address": sole_proprietor,
            "contact": contact,
        },
    )


class NpiRegistrySource:
    """CMS NPI Registry: licensed practices by specialty."""

    key = "npi_registry"

    def __init__(self, transport: Optional[httpx.BaseTransport] = None, pause: float = 0.2):
        self.transport = transport
        self.pause = pause

    def is_configured(self) -> bool:
        return True  # free public data

    def discover(self, filters: dict) -> Iterator[Prospect]:
        taxonomies = filters.get("npi_taxonomies") or ["Dentist", "Chiropractor"]
        wanted = {t.lower() for t in taxonomies}
        enumeration = NPI_TYPES.get(filters.get("npi_type", "organization"), "NPI-2")
        places = [("postal_code", z) for z in filters.get("postal_codes") or []] + \
                 [("city", c) for c in filters.get("cities") or []] or [(None, None)]
        seen: set[str] = set()

        with httpx.Client(timeout=30, transport=self.transport,
                          headers={"User-Agent": "agency-os prospect source"}) as client:
            for taxonomy in taxonomies:
                for place_field, place in places:
                    params = {"version": "2.1", "taxonomy_description": taxonomy,
                              "enumeration_type": enumeration, "limit": PAGE}
                    if filters.get("state"):
                        params["state"] = filters["state"]
                    if place_field:
                        params[place_field] = place
                    for prospect in self._search(client, params, wanted, (place_field, place)):
                        if prospect.metadata["external_ref"] not in seen:
                            seen.add(prospect.metadata["external_ref"])
                            yield prospect

    def _search(self, client: httpx.Client, params: dict, wanted: set[str], place: tuple) -> Iterator[Prospect]:
        skip = 0
        while skip <= MAX_SKIP:
            resp = client.get(API_URL, params={**params, "skip": skip})
            resp.raise_for_status()
            body = resp.json()
            if body.get("Errors"):
                raise ValueError(f"NPI Registry: {body['Errors'][0].get('description', 'search rejected')}")
            results = body.get("results") or []
            for result in results:
                prospect = to_prospect(result, wanted, self.key, place)
                if prospect:
                    yield prospect
            if len(results) < PAGE:
                return
            skip += PAGE
            time.sleep(self.pause)
        where = params.get("city") or params.get("postal_code") or params.get("state") or "everywhere"
        print(f"    ! NPI Registry capped {params['taxonomy_description']} in {where} at "
              f"{MAX_SKIP + PAGE}; split it into more cities or postal_codes")
