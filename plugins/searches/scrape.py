"""
Scrape search: organizations listed on a directory page and its "next" pages,
all on the same host (docs/U9ITUS_BILLING.md, task B7).

params: {
  "url": "https://chamber.example.org/members",
  "item_selector": ".member",                     # one element per organization
  "fields": {"name": "h3", "website": "a.site@href", "phone": ".tel"},
  "next_selector": "a.next@href",                 # optional
  "max_pages": 5                                  # optional, 1-20
}

A field is a CSS selector inside the item, optionally with @attr to read an
attribute instead of the text ("a@href"); "@attr" alone reads the item's own
attribute. `name` is required. Every fetch goes through ctx.fetch
(core/safe_fetch.py): robots.txt, 1 request/s per host, no internal addresses.
"""

from __future__ import annotations

import hashlib
import re
from typing import Iterator, Optional
from urllib.parse import urljoin, urlsplit

from selectolax.lexbor import LexborHTMLParser as HTMLParser

from core.models import Prospect
from core.safe_fetch import same_host

FIELDS = ("name", "website", "address", "city", "state", "zip", "phone", "focus_area")
MAX_PAGES = 20
DEFAULT_PAGES = 5
_SELECTOR_MAX = 200
_ATTR = re.compile(r"^[A-Za-z_:][A-Za-z0-9_:.-]{0,40}$")


def _split(spec: str) -> tuple[str, Optional[str]]:
    """"a.site@href" → ("a.site", "href"); "h3" → ("h3", None); "@href" → ("", "href")."""
    selector, at, attr = spec.rpartition("@") if "@" in spec else (spec, "", "")
    return selector.strip(), (attr.strip() or None) if at else None


def _check_selector(spec, where: str, allow_attr: bool = True) -> str:
    spec = " ".join(str(spec or "").split())
    if not spec or len(spec) > _SELECTOR_MAX:
        raise ValueError(f"{where} must be a CSS selector of at most {_SELECTOR_MAX} characters.")
    selector, attr = _split(spec)
    if attr is not None and (not allow_attr or not _ATTR.match(attr)):
        raise ValueError(f"{where}: after @ put an attribute name, e.g. a@href.")
    if selector:
        try:
            HTMLParser("<p></p>").css(selector)
        except Exception:
            raise ValueError(f"{where} isn't a valid CSS selector: {selector}") from None
    elif attr is None:
        raise ValueError(f"{where} must be a CSS selector.")
    return spec


def _value(node, spec: str) -> str:
    selector, attr = _split(spec)
    target = node.css_first(selector) if selector else node
    if target is None:
        return ""
    raw = target.attributes.get(attr) if attr else target.text(separator=" ")
    return " ".join((raw or "").split())


class ScrapeSearch:
    key = "scrape"

    def validate(self, params: dict) -> dict:
        url = str(params.get("url") or "").strip()
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.hostname or len(url) > 2000:
            raise ValueError("url must be a full http:// or https:// web address.")
        fields = params.get("fields")
        if not isinstance(fields, dict) or not fields.get("name"):
            raise ValueError("fields must say where each value is, and include name, "
                             'e.g. {"name": "h3", "website": "a@href"}.')
        unknown = sorted(set(fields) - set(FIELDS))
        if unknown:
            raise ValueError(f"fields can only be: {', '.join(FIELDS)} (not {', '.join(unknown)}).")
        cleaned = {
            "url": url,
            "item_selector": _check_selector(params.get("item_selector"), "item_selector", allow_attr=False),
            "fields": {k: _check_selector(v, f"fields.{k}") for k, v in fields.items() if str(v or "").strip()},
        }
        if params.get("next_selector"):
            cleaned["next_selector"] = _check_selector(params["next_selector"], "next_selector")
        pages = params.get("max_pages", DEFAULT_PAGES)
        if isinstance(pages, bool) or not isinstance(pages, int) or not 1 <= pages <= MAX_PAGES:
            raise ValueError(f"max_pages must be a whole number from 1 to {MAX_PAGES}.")
        cleaned["max_pages"] = pages
        return cleaned

    def _prospect(self, item, fields: dict, page_url: str) -> Optional[Prospect]:
        values = {k: _value(item, spec)[:300] for k, spec in fields.items()}
        name = values.get("name", "")[:200]
        if not name:
            return None
        website = values.get("website") or None
        if website:
            website = urljoin(page_url, website)
            if urlsplit(website).scheme not in ("http", "https"):
                website = None
        host = (urlsplit(page_url).hostname or "").lower()
        ref = hashlib.sha1(f"{host}|{name.lower()}".encode()).hexdigest()[:16]
        state = values.get("state", "").upper()
        return Prospect(
            name=name, website_url=website, address=values.get("address") or None,
            city=values.get("city") or None, state=state[:2] if len(state) == 2 else None,
            zip=values.get("zip") or None, focus_area=values.get("focus_area") or None,
            source="scrape", source_url=page_url,
            metadata={"external_ref": f"{host}:{ref}", "phone": values.get("phone") or None},
        )

    def run(self, params: dict, ctx) -> Iterator[Prospect]:
        url, seen_pages, found = params["url"], set(), 0
        for _ in range(params["max_pages"]):
            seen_pages.add(url)
            page = ctx.fetch.get(url)
            ctx.count(pages_fetched=1)
            tree = HTMLParser(page.text)
            for item in tree.css(params["item_selector"]):
                prospect = self._prospect(item, params["fields"], page.url)
                if prospect is not None:
                    found += 1
                    yield prospect
            spec = params.get("next_selector")
            if not spec:
                break
            selector, attr = _split(spec)
            link = tree.css_first(selector) if selector else None
            href = (link.attributes.get(attr or "href") if link is not None else None) or ""
            next_url = urljoin(page.url, href.strip()) if href.strip() else ""
            if not next_url or next_url in seen_pages or not same_host(next_url, params["url"]):
                break
            url = next_url
        if not found:
            raise ValueError("No organizations matched item_selector and fields.name on that page. "
                             "Check the selectors against the page's HTML.")
