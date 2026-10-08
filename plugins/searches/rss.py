"""
RSS search: the organizations behind a feed's items (docs/U9ITUS_BILLING.md, task B6).

params: {"feed_url": "https://news.example.org/feed.xml",
         "keywords": ["clinic", "dental"],      # optional: list or "a, b"; any one must match
         "since": "2026-09-01"}                 # optional: skip items older than this

Each matching item gives one organization: the publisher named in the item's
<source> when the feed has one (news aggregators do), else the site the item
links to. One prospect per site. RSS 2.0 and Atom are read; the feed is
fetched through ctx.fetch (core/safe_fetch.py).
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from datetime import date, datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Iterator, Optional
from urllib.parse import urlsplit

from core.models import Prospect

MAX_KEYWORDS = 10
ATOM = "{http://www.w3.org/2005/Atom}"
_DOCTYPE = re.compile(rb"<!(DOCTYPE|ENTITY)", re.I)
UNREADABLE = "That feed couldn't be read as RSS or Atom."


def _text(el: Optional[ET.Element]) -> str:
    return " ".join("".join(el.itertext()).split()) if el is not None else ""


def _origin(url: str) -> Optional[str]:
    parts = urlsplit(url.strip())
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return None
    return f"{parts.scheme}://{parts.hostname.lower()}"


def _when(raw: str) -> Optional[datetime]:
    raw = raw.strip()
    if not raw:
        return None
    try:
        found = parsedate_to_datetime(raw)
    except (TypeError, ValueError, IndexError):
        try:
            found = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return None
    return found if found.tzinfo else found.replace(tzinfo=timezone.utc)


def parse_feed(content: bytes) -> list[dict]:
    """[{title, link, summary, published, source_name, source_url}] from RSS 2.0 or Atom, or ValueError."""
    if _DOCTYPE.search(content[:4096]):
        raise ValueError(UNREADABLE)  # no DTDs or entities: nothing to expand
    try:
        root = ET.fromstring(content)
    except ET.ParseError:
        raise ValueError(UNREADABLE) from None
    items = []
    if root.tag == f"{ATOM}feed":
        for entry in root.findall(f"{ATOM}entry"):
            links = entry.findall(f"{ATOM}link")
            link = next((l.get("href", "") for l in links if l.get("rel", "alternate") == "alternate"), "")
            source = entry.find(f"{ATOM}source")
            items.append({
                "title": _text(entry.find(f"{ATOM}title")), "link": link,
                "summary": _text(entry.find(f"{ATOM}summary")) or _text(entry.find(f"{ATOM}content")),
                "published": _when(_text(entry.find(f"{ATOM}published")) or _text(entry.find(f"{ATOM}updated"))),
                "source_name": _text(source.find(f"{ATOM}title")) if source is not None else "",
                "source_url": next((l.get("href", "") for l in source.findall(f"{ATOM}link")), "")
                if source is not None else "",
            })
        return items
    channel = root.find("channel") if root.tag == "rss" else (root if root.tag.endswith("RDF") else None)
    if channel is None:
        raise ValueError(UNREADABLE)
    for item in channel.iter("item"):
        source = item.find("source")
        items.append({
            "title": _text(item.find("title")), "link": _text(item.find("link")),
            "summary": _text(item.find("description")),
            "published": _when(_text(item.find("pubDate"))),
            "source_name": _text(source), "source_url": source.get("url", "") if source is not None else "",
        })
    return items


class RssSearch:
    key = "rss"
    label = "Organizations in a news feed"
    description = "The organizations behind a feed's stories: the publisher, or the site each story links to."
    fields = [
        {"name": "feed_url", "label": "Feed address", "type": "url", "required": True,
         "example": "https://news.example.org/feed.xml", "help": "An RSS or Atom feed."},
        {"name": "keywords", "label": "Keywords", "type": "list", "required": False,
         "example": ["clinic", "dental"], "help": f"Only stories mentioning any of these; at most {MAX_KEYWORDS}."},
        {"name": "since", "label": "Stories since", "type": "date", "required": False, "example": "2026-09-01"},
    ]
    attribution = None

    def validate(self, params: dict) -> dict:
        feed_url = str(params.get("feed_url") or "").strip()
        if _origin(feed_url) is None or len(feed_url) > 2000:
            raise ValueError("feed_url must be a full http:// or https:// web address.")
        raw = params.get("keywords") or []
        if isinstance(raw, str):
            raw = raw.split(",")
        if not isinstance(raw, list):
            raise ValueError("keywords must be a list of words, or one string separated by commas.")
        keywords = [" ".join(str(k).split())[:60] for k in raw if str(k).strip()]
        if len(keywords) > MAX_KEYWORDS:
            raise ValueError(f"Use at most {MAX_KEYWORDS} keywords.")
        cleaned: dict = {"feed_url": feed_url}
        if keywords:
            cleaned["keywords"] = keywords
        if params.get("since"):
            try:
                cleaned["since"] = date.fromisoformat(str(params["since"]).strip()[:10]).isoformat()
            except ValueError:
                raise ValueError("since must be a date like 2026-09-01.") from None
        return cleaned

    def run(self, params: dict, ctx) -> Iterator[Prospect]:
        page = ctx.fetch.get(params["feed_url"])
        ctx.count(pages_fetched=1)
        items = parse_feed(page.content)
        keywords = [k.lower() for k in params.get("keywords", [])]
        since = datetime.fromisoformat(params["since"]).replace(tzinfo=timezone.utc) if params.get("since") else None
        seen: set[str] = set()
        for item in items:
            haystack = f"{item['title']} {item['summary']}".lower()
            if keywords and not any(k in haystack for k in keywords):
                continue
            if since and item["published"] and item["published"] < since:
                continue
            site = _origin(item["source_url"]) or _origin(item["link"])
            if site is None or site in seen:
                continue
            seen.add(site)
            host = urlsplit(site).hostname or ""
            name = item["source_name"] or (host[4:] if host.startswith("www.") else host)
            yield Prospect(
                name=name[:200], website_url=site, source="rss", source_url=item["link"][:2000] or None,
                metadata={"external_ref": site, "found_in": item["title"][:300]},
            )
