"""
City search: businesses and organizations of one kind in a US city, from
OpenStreetMap through the Overpass API (free; no key).

params: {"city": "Austin", "state": "TX", "query": "dentist"}

`query` matches OpenStreetMap's category tags (amenity, shop, office, craft,
healthcare: "dentist", "cafe", "lawyer", "hairdresser"...) and, failing that,
names containing it. Coverage is good for chains and city centers and thinner
for small businesses; a paid provider (Google Places) is decision 8.1 in
docs/U9ITUS_BILLING.md.

Overpass is shared and rate limited: one request per search, a server-side
timeout, and AGENCY_OS_OVERPASS_URL to point at our own instance if we outgrow it.
"""

from __future__ import annotations

import os
import re
from typing import Iterator

from core.models import Prospect

OVERPASS_URL = "https://overpass-api.de/api/interpreter"
STATES = {
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "DC", "FL", "GA", "HI", "ID", "IL", "IN", "IA", "KS",
    "KY", "LA", "ME", "MD", "MA", "MI", "MN", "MS", "MO", "MT", "NE", "NV", "NH", "NJ", "NM", "NY", "NC",
    "ND", "OH", "OK", "OR", "PA", "RI", "SC", "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV", "WI", "WY",
}
CATEGORY_TAGS = ("amenity", "shop", "office", "craft", "healthcare")
# Only these characters reach the Overpass query, so nothing in it can be escaped out of.
_CITY = re.compile(r"^[A-Za-z][A-Za-z .'-]{0,79}$")
_QUERY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 &'-]{1,59}$")


def _category(query: str) -> str:
    """'Dentists' → 'dentist', 'hair salon' → 'hair_salon' (OpenStreetMap tag style)."""
    value = re.sub(r"[^a-z0-9]+", "_", query.lower()).strip("_")
    return value[:-1] if value.endswith("s") and not value.endswith("ss") else value


def build_query(city: str, state: str, query: str, limit: int) -> str:
    category = _category(query)
    name = re.sub(r"[^A-Za-z0-9 ]", ".", query)  # & ' - match any character; no escaping needed
    wanted = "".join(f'  nwr(area.city)["name"]["{tag}"="{category}"];\n' for tag in CATEGORY_TAGS)
    return (
        "[out:json][timeout:60];\n"
        f'area["ISO3166-2"="US-{state}"]["admin_level"="4"]->.state;\n'
        f'rel(area.state)["boundary"="administrative"]["name"="{city}"];\n'
        "map_to_area->.city;\n"
        "(\n"
        f"{wanted}"
        f'  nwr(area.city)["name"~"{name}",i];\n'
        ");\n"
        f"out tags center {limit};\n"
    )


def _prospect(element: dict, city: str, state: str, category: str) -> Prospect | None:
    tags = element.get("tags") or {}
    name = (tags.get("name") or "").strip()
    if not name:
        return None
    street = " ".join(filter(None, [tags.get("addr:housenumber"), tags.get("addr:street")])) or None
    ref = f"{element.get('type')}/{element.get('id')}"
    found_as = next((tags[t] for t in CATEGORY_TAGS if tags.get(t)), category)
    return Prospect(
        name=name[:200],
        website_url=tags.get("website") or tags.get("contact:website"),
        address=street,
        city=tags.get("addr:city") or city,
        state=state,
        zip=tags.get("addr:postcode"),
        focus_area=found_as.replace("_", " "),
        source="osm_city",
        source_url=f"https://www.openstreetmap.org/{ref}",
        metadata={"external_ref": ref,
                  "phone": tags.get("phone") or tags.get("contact:phone"),
                  "license": "© OpenStreetMap contributors, ODbL"},
    )


class CitySearch:
    key = "city"

    def validate(self, params: dict) -> dict:
        city = " ".join(str(params.get("city") or "").split())
        state = str(params.get("state") or "").strip().upper()
        query = " ".join(str(params.get("query") or "").split())
        if not _CITY.match(city):
            raise ValueError("city must be a city name (letters, spaces, . ' -).")
        if state not in STATES:
            raise ValueError("state must be a two-letter US state code, e.g. TX.")
        if not _QUERY.match(query):
            raise ValueError("query must be 2-60 letters, digits, spaces, & ' or -, e.g. dentist.")
        return {"city": city, "state": state, "query": query}

    def run(self, params: dict, ctx) -> Iterator[Prospect]:
        # Ask for extra: some results have no name or are already the account's.
        limit = min(1000, ctx.remaining * 2 + 20)
        query = build_query(params["city"], params["state"], params["query"], limit)
        response = ctx.http.post(os.environ.get("AGENCY_OS_OVERPASS_URL") or OVERPASS_URL, data={"data": query})
        ctx.count(api_requests=1)
        if response.status_code in (429, 504):
            raise ValueError("The map data service is busy. Try the search again in a few minutes.")
        response.raise_for_status()
        elements = response.json().get("elements") or []
        if not elements:
            raise ValueError(f"Nothing found for {params['query']!r} in {params['city']}, {params['state']}. "
                             "Check the city's spelling or try a broader query.")
        category = _category(params["query"])
        for element in elements:
            prospect = _prospect(element, params["city"], params["state"], category)
            if prospect is not None:
                yield prospect
