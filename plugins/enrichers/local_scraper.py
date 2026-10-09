"""
Local web scraper enricher — no API keys needed.

For each prospect:
  1. If we already have a website_url (from IRS BMF), use it.
  2. If not, try common domain patterns (orgname.org, orgname.com, etc.)
     and a DuckDuckGo search as a fallback.
  3. Fetch the homepage + /contact, /about, /staff, /team pages.
  4. Extract phone numbers, email addresses, and staff/contact names
     using regex patterns.

This is slow (fetches multiple pages per prospect) but free and local.
Run it overnight or in batches with --limit.
"""

from __future__ import annotations

import re
import time
from typing import Optional
from urllib.parse import urljoin, urlparse

import httpx
from selectolax.lexbor import LexborHTMLParser as HTMLParser

from core.models import Prospect, EnrichmentResult


# ── Regex patterns ──────────────────────────────────────────────────

# US phone number: (213) 555-0100, 213-555-0100, 213.555.0100, +1 213 555 0100
PHONE_RE = re.compile(
    r"(?:\+?1[\s.\-]?)?"                     # optional country code
    r"\(?(\d{3})\)?"                          # area code
    r"[\s.\-]"                                # separator
    r"(\d{3})"                                # prefix
    r"[\s.\-]"                                # separator
    r"(\d{4})"                                # line number
)

# Email address
EMAIL_RE = re.compile(
    r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}"
)

# Staff/title patterns — "Jane Doe, Executive Director" or "Executive Director: Jane Doe"
# Also catches "Jane Doe (Executive Director)"
TITLE_PATTERNS = [
    "executive director",
    "program director",
    "outreach director",
    "development director",
    "executive director",
    "deputy director",
    "communications director",
    "community outreach",
    "program manager",
    "executive coordinator",
    "office manager",
    "coordinator",
    "founder",
    "ceo",
    "president",
    "executive",
]

# Build a regex that matches "Name, Title" or "Title: Name" or "Name (Title)"
TITLE_RE = re.compile(
    r"(?:"
    # "Title: First Last"
    r"(?:" + "|".join(TITLE_PATTERNS) + r")\s*[:\-]\s*([A-Z][a-z]+(?:\s+[A-Z][a-z]+){1,3})"
    r"|"
    # "First Last, Title"
    r"([A-Z][a-z]+(?:\s+[A-Z][a-z]+){1,3})\s*[,–—]\s*(?:" + "|".join(TITLE_PATTERNS) + r")"
    r"|"
    # "First Last (Title)"
    r"([A-Z][a-z]+(?:\s+[A-Z][a-z]+){1,3})\s*\((?:" + "|".join(TITLE_PATTERNS) + r")\)"
    r")",
    re.IGNORECASE,
)

# Pages to check for contact info (appended to the base URL)
CONTACT_PATHS = [
    "/contact",
    "/contact-us",
    "/about",
    "/about-us",
    "/staff",
    "/team",
    "/leadership",
    "/about/contact",
    "/about/staff",
    "/who-we-are",
]

# Common TLDs to try for domain guessing
DOMAIN_TLDS = [".org", ".com", ".net"]

# Junk emails to filter out
JUNK_EMAIL_DOMAINS = {
    "sentry.io", "wixpress.com", "example.com", "domain.com",
    "yourdomain.com", "email.com", "gmail.com",  # generic gmail often not the right contact
    "yahoo.com", "hotmail.com", "outlook.com",
    "godaddy.com", "wordpress.com", "squarespace.com",
    "shopify.com", "mailchimp.com", "constantcontact.com",
}

# Pages that describe what the org does, and the most of it we keep
ABOUT_PATH_HINTS = ("about", "who-we-are")
SUMMARY_MAX = 500
SUMMARY_JUNK = ("cookie", "copyright", "©", "all rights reserved", "javascript")

# Junk phone prefixes (toll-free, info lines)
# The org's own YouTube channel or video, linked from its site. A channel is
# preferred: u9itus picks its latest embeddable upload for the video demo.
YOUTUBE_CHANNEL_RE = re.compile(
    r"https?://(?:www\.)?youtube\.com/(?:@[\w.-]{3,30}|channel/UC[\w-]{22}|c/[\w.-]+|user/[\w.-]+)", re.I)
YOUTUBE_VIDEO_RE = re.compile(
    r"https?://(?:www\.|m\.)?(?:youtube(?:-nocookie)?\.com/(?:watch\?v=|embed/|shorts/)|youtu\.be/)[\w-]{11}", re.I)

JUNK_PHONE_PREFIXES = {"800", "888", "877", "866", "855", "844", "833", "000", "555"}


class LocalScraperEnricher:
    """Scrape org websites for contact info — no API key needed."""

    key = "local_scraper"

    def __init__(self):
        self._client = httpx.Client(
            timeout=15,
            follow_redirects=True,
            headers={
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                              "AppleWebKit/537.36 (KHTML, like Gecko) "
                              "Chrome/125.0.0.0 Safari/537.36"
            },
        )

    def is_configured(self) -> bool:
        return True  # no keys needed, just needs internet

    def enrich(self, prospect: Prospect) -> EnrichmentResult:
        """Find website, scrape it for phone/email/contact name."""
        result = EnrichmentResult(source=self.key, confidence=0.5)

        # Step 1: Determine the website URL
        website = self._find_website(prospect)
        if not website:
            return result

        result.raw["website"] = website

        # Step 2: Fetch homepage + contact pages
        pages = self._fetch_pages(website)
        if not pages:
            return result

        # Step 3: Extract contact info from all pages
        all_text = ""
        for url, html in pages:
            text = self._extract_text(html)
            all_text += "\n" + text

        # Extract phone
        phone = self._extract_phone(all_text)
        if not phone:
            phone = self._extract_phone_from_html(pages)
        if phone:
            result.contact_phone = phone
            result.confidence = min(result.confidence + 0.2, 1.0)

        # Extract email
        email = self._extract_email(all_text, website)
        if not email:
            email = self._extract_email_from_html(pages, website)
        if email:
            result.contact_email = email
            result.confidence = min(result.confidence + 0.2, 1.0)

        # Extract contact name (someone with a title)
        name = self._extract_contact_name(all_text)
        if name:
            result.contact_name = name
            result.confidence = min(result.confidence + 0.1, 1.0)

        # What the org says it does, for call scripts and the AI
        summary = self._extract_summary(pages)
        if summary:
            result.raw["site_summary"] = summary

        # Their own video, for the u9itus video campaign demo (no extra requests)
        youtube = self._extract_youtube(pages)
        if youtube:
            result.raw["youtube_url"] = youtube

        return result

    # ── Website discovery ────────────────────────────────────────────

    def _find_website(self, prospect: Prospect) -> Optional[str]:
        """Find the org's website — use known URL or try to discover one."""
        # If we already have one from IRS data, use it
        if prospect.website_url:
            url = prospect.website_url.strip()
            if not url.startswith("http"):
                url = "https://" + url
            return url

        # Try domain guessing with multiple slug strategies
        slug_candidates = self._org_slug_candidates(prospect.name)
        for slug in slug_candidates:
            for tld in DOMAIN_TLDS:
                candidate = f"https://{slug}{tld}"
                if self._url_exists(candidate):
                    # Verify this is actually the right org by checking page title
                    if self._verify_org(candidate, prospect.name):
                        return candidate
                    time.sleep(0.2)

        # Fallback: DuckDuckGo search (HTML, no API key)
        return self._search_ddg(prospect)

    def _org_slug_candidates(self, name: str) -> list[str]:
        """Generate multiple domain slug candidates from org name."""
        # Remove common suffixes
        suffixes = [
            " inc", " incorporated", " llc", " corp", " corporation",
            " foundation", " coalition", " center", " centre",
            " of california", " of los angeles", " of greater los angeles",
            " of southern california", " usa", " us", " inc.",
            " community development corporation", " development corporation",
            " resource center", " law center", " workers center",
        ]
        slug = name.lower().strip()
        for s in suffixes:
            if slug.endswith(s):
                slug = slug[: -len(s)].strip()

        if slug.startswith("the "):
            slug = slug[4:]

        words = re.sub(r"[^a-z0-9\s]", "", slug).split()
        stop_words = {"of", "the", "for", "and", "in", "la", "los", "angeles", "ca", "california", "a", "an"}
        meaningful = [w for w in words if w not in stop_words]

        if not meaningful:
            return []

        candidates = []
        seen = set()

        def add(s):
            if s and s not in seen:
                seen.add(s)
                candidates.append(s)

        # Strategy 1: All meaningful words joined (e.g. "koreanyouthcommunitycenter")
        if len(meaningful) >= 2:
            add("".join(meaningful[:3]))

        # Strategy 2: First two meaningful words joined (e.g. "koreanyouth")
        if len(meaningful) >= 2:
            add(f"{meaningful[0]}{meaningful[1]}")

        # Strategy 3: First word only (e.g. "carecen", "chirla")
        add(meaningful[0])

        # Strategy 4: Acronym (e.g. "CARECEN" → already handled, "LA LGBT Center" → "lalgbtcenter")
        if len(words) >= 2:
            acronym = "".join(w[0] for w in words if w and w[0].isalpha() and w not in stop_words)
            if len(acronym) >= 3:
                add(acronym.lower())

        # Strategy 5: First meaningful word + "la" or "ca" (e.g. "chirlala")
        # Less common but sometimes used

        return candidates

    def _verify_org(self, url: str, org_name: str) -> bool:
        """Quick check that the page mentions the org name. Relaxed — checks
        title OR first heading OR meta description for any significant word."""
        try:
            resp = self._client.get(url, timeout=10)
            if resp.status_code != 200:
                return False
            tree = HTMLParser(resp.text)

            # Gather text from title, h1, meta description
            check_text = ""
            title = tree.css_first("title")
            if title:
                check_text += " " + title.text(strip=True)
            h1 = tree.css_first("h1")
            if h1:
                check_text += " " + h1.text(strip=True)
            for meta in tree.css('meta[name="description"], meta[property="og:description"]'):
                check_text += " " + (meta.attributes.get("content") or "")

            check_text = check_text.lower()
            org_lower = org_name.lower()

            # Check if the org name or significant parts appear
            org_words = [w.lower() for w in org_name.split()
                         if len(w) > 3 and w.lower() not in
                         {"los", "angeles", "california", "center", "inc",
                          "foundation", "coalition", "corporation", "incorporated"}]

            if not org_words:
                return True  # can't verify, allow it

            matches = sum(1 for w in org_words if w in check_text)
            # Need at least 1 significant word match, or 30% of words
            threshold = max(1, len(org_words) // 3)
            return matches >= threshold
        except Exception:
            return False

    def _url_exists(self, url: str) -> bool:
        """Quick check if a URL resolves."""
        try:
            resp = self._client.head(url, timeout=8)
            return resp.status_code < 400
        except Exception:
            return False

    def _search_ddg(self, prospect: Prospect) -> Optional[str]:
        """Search DuckDuckGo HTML for the org's website."""
        # Try multiple search queries
        queries = [
            f'"{prospect.name}" {prospect.city or ""} CA',
            f'{prospect.name} {prospect.city or "Los Angeles"} California website',
        ]

        for query in queries:
            query = query.strip()
            if not query:
                continue
            try:
                resp = self._client.get(
                    "https://html.duckduckgo.com/html/",
                    params={"q": query},
                    timeout=15,
                )
                if resp.status_code != 200:
                    continue

                tree = HTMLParser(resp.text)
                # DDG HTML results have links in .result__a
                for a in tree.css(".result__a"):
                    href = a.attributes.get("href", "")
                    # DDG wraps links in a redirect — extract the actual URL
                    if "uddg=" in href:
                        from urllib.parse import parse_qs, unquote
                        parsed = urlparse(href if href.startswith("http") else "https:" + href)
                        qs = parse_qs(parsed.query)
                        if "uddg" in qs:
                            actual_url = unquote(qs["uddg"][0])
                            domain = urlparse(actual_url).netloc.lower()
                            skip_domains = [
                                "facebook.com", "twitter.com", "x.com", "instagram.com",
                                "linkedin.com", "youtube.com", "yelp.com", "wikipedia.org",
                                "guidestar.org", "charitynavigator.org", "propublica.org",
                                "irs.gov", "bloomerang", "donorperfect", "taxexemptworld",
                                "californiastateauthority", "mytownview", "openstreetmap",
                                "google.com", "bing.com", "bizprofile.net", "lacounty.gov",
                                "laassubject.org", "marginalrevolution.com",
                            ]
                            if any(d in domain for d in skip_domains):
                                continue
                            # Also skip directory/profile pages
                            if any(p in actual_url.lower() for p in ["/directory/", "/profile/", "/listing/", "/org/", "/organization/"]):
                                continue
                            # Prefer .org domains for nonprofits
                            if actual_url.startswith("http"):
                                return actual_url
                time.sleep(1)  # be polite between queries
            except Exception:
                continue

        return None

    # ── Page fetching ────────────────────────────────────────────────

    def _fetch_pages(self, base_url: str) -> list[tuple[str, str]]:
        """Fetch homepage + contact pages. Returns [(url, html), ...]."""
        pages = []
        urls_to_try = [base_url]

        # Add contact/about pages
        parsed = urlparse(base_url)
        base_origin = f"{parsed.scheme}://{parsed.netloc}"
        for path in CONTACT_PATHS:
            urls_to_try.append(urljoin(base_origin, path))

        for url in urls_to_try:
            try:
                resp = self._client.get(url, timeout=10)
                if resp.status_code == 200 and "text/html" in resp.headers.get("content-type", ""):
                    pages.append((url, resp.text))
                    time.sleep(0.3)  # be polite
            except Exception:
                continue

        return pages

    # ── Extraction ───────────────────────────────────────────────────

    def _extract_text(self, html: str) -> str:
        """Extract visible text from HTML, focusing on contact-relevant areas."""
        tree = HTMLParser(html)

        # Remove script, style, nav, footer noise
        for tag in tree.css("script, style, nav, noscript"):
            tag.decompose()

        # Prioritize text from contact-relevant elements
        priority_selectors = [
            "footer", ".footer", "#footer",
            ".contact", "#contact", ".contact-info",
            ".staff", ".team", ".leadership",
            ".about", "#about",
            "address",
        ]

        priority_text = ""
        for sel in priority_selectors:
            for el in tree.css(sel):
                priority_text += " " + el.text(separator=" ")

        # Also get full body text as fallback
        body = tree.css_first("body")
        body_text = body.text(separator=" ") if body else ""

        return priority_text + " " + body_text

    def _extract_summary(self, pages: list[tuple[str, str]]) -> Optional[str]:
        """A few sentences on what the org does: the about page's opening
        paragraphs, else the homepage's meta description, else its paragraphs."""
        about = [html for url, html in pages
                 if any(h in urlparse(url).path.lower() for h in ABOUT_PATH_HINTS)]
        home = pages[0][1] if pages else ""
        for text in [self._paragraphs(html) for html in about] + [
            self._meta_description(home), self._paragraphs(home),
        ]:
            if text:
                return self._clip(text)
        return None

    def _paragraphs(self, html: str) -> str:
        """The page's first substantial paragraphs, skipping legal/cookie boilerplate."""
        kept = []
        for p in HTMLParser(html).css("p"):
            text = " ".join(p.text(separator=" ").split())
            if len(text) < 80 or any(j in text.lower() for j in SUMMARY_JUNK):
                continue
            kept.append(text)
            if sum(len(t) for t in kept) >= SUMMARY_MAX:
                break
        return " ".join(kept)

    def _meta_description(self, html: str) -> str:
        tree = HTMLParser(html)
        for sel in ('meta[name="description"]', 'meta[property="og:description"]'):
            el = tree.css_first(sel)
            content = " ".join((el.attributes.get("content") or "").split()) if el else ""
            if len(content) >= 40:
                return content
        return ""

    def _clip(self, text: str) -> str:
        """Trim to SUMMARY_MAX, ending on a sentence (or word) boundary."""
        if len(text) <= SUMMARY_MAX:
            return text
        cut = text[:SUMMARY_MAX]
        end = cut.rfind(". ")
        return cut[:end + 1] if end > SUMMARY_MAX // 2 else cut.rsplit(" ", 1)[0] + "…"

    def _extract_youtube(self, pages: list[tuple[str, str]]) -> Optional[str]:
        """First YouTube channel link on the fetched pages, else the first video link."""
        html = "\n".join(page_html for _, page_html in pages)
        for pattern in (YOUTUBE_CHANNEL_RE, YOUTUBE_VIDEO_RE):
            match = pattern.search(html)
            if match:
                return match.group(0).replace("youtube-nocookie.com", "youtube.com")
        return None

    def _extract_phone(self, text: str) -> Optional[str]:
        """Extract the first valid phone number."""
        # Look near "tel:" links first (most reliable)
        tel_match = re.search(r'tel:(\+?1?[\d\s\-\.\(\)]+)', text)
        if tel_match:
            phone = self._normalize_phone(tel_match.group(1))
            if phone:
                return phone

        # Regex match
        for match in PHONE_RE.finditer(text):
            raw = match.group(0)
            phone = self._normalize_phone(raw)
            if phone and phone[:3] not in JUNK_PHONE_PREFIXES:
                return phone

        return None

    def _extract_phone_from_html(self, pages: list[tuple[str, str]]) -> Optional[str]:
        """Fallback: scan raw HTML for tel: links (JS-hidden / obfuscated numbers)."""
        for _url, page_html in pages:
            for match in re.finditer(r'href\s*=\s*["\']tel:([^"\']+)["\']', page_html, re.IGNORECASE):
                phone = self._normalize_phone(match.group(1))
                if phone and phone[:3] not in JUNK_PHONE_PREFIXES:
                    return phone
        return None

    def _extract_email_from_html(self, pages: list[tuple[str, str]], website: str) -> Optional[str]:
        """Fallback: scan raw HTML for mailto: links when visible text has none.

        Applies the same junk-domain and org-domain ranking as visible-text
        extraction.
        """
        org_domain = urlparse(website).netloc.lower().replace("www.", "")
        org_emails: list[str] = []
        priority_emails: list[str] = []
        other_emails: list[str] = []
        priority_locals = ("info", "contact", "admin", "office", "hello", "mail", "director", "ed")

        for _url, page_html in pages:
            for match in re.finditer(
                r'href\s*=\s*["\']mailto:([^?"\']+)[^"\']*["\']', page_html, re.IGNORECASE
            ):
                email = match.group(1).strip().lower()
                if not EMAIL_RE.fullmatch(email):
                    continue
                local, _, domain = email.partition("@")
                if domain in JUNK_EMAIL_DOMAINS:
                    continue
                if org_domain and domain == org_domain:
                    org_emails.append(email)
                elif local in priority_locals:
                    priority_emails.append(email)
                else:
                    other_emails.append(email)

        for pool in (org_emails, priority_emails, other_emails):
            if pool:
                for pref in priority_locals:
                    for e in pool:
                        if e.startswith(pref + "@"):
                            return e
                return pool[0]
        return None

    def _normalize_phone(self, raw: str) -> Optional[str]:
        """Normalize to (XXX) XXX-XXXX format."""
        digits = re.sub(r"[^\d]", "", raw)
        # Remove leading 1 if 11 digits
        if len(digits) == 11 and digits.startswith("1"):
            digits = digits[1:]
        if len(digits) != 10:
            return None
        area, prefix, line = digits[:3], digits[3:6], digits[6:]
        if area in JUNK_PHONE_PREFIXES or prefix in JUNK_PHONE_PREFIXES:
            return None
        return f"({area}) {prefix}-{line}"

    def _extract_email(self, text: str, website: str) -> Optional[str]:
        """Extract the best email address (prefer info@, contact@, director@)."""
        emails = EMAIL_RE.findall(text)
        if not emails:
            return None

        # Filter junk
        domain = urlparse(website).netloc.lower()
        org_domain = domain.replace("www.", "")

        # Categorize emails
        priority_emails = []
        org_emails = []
        other_emails = []

        for email in emails:
            email_lower = email.lower()
            local, _, email_domain = email_lower.partition("@")

            # Skip junk domains
            if email_domain in JUNK_EMAIL_DOMAINS:
                continue
            # Skip image/CSS filenames that look like emails
            if any(email_lower.endswith(ext) for ext in [".png", ".jpg", ".gif", ".css", ".js"]):
                continue

            # Prefer emails at the org's own domain
            if org_domain and email_domain == org_domain:
                org_emails.append(email_lower)
            # Prefer generic contact addresses
            elif local in ("info", "contact", "admin", "office", "hello", "mail", "director", "ed"):
                priority_emails.append(email_lower)
            else:
                other_emails.append(email_lower)

        # Return best match
        # 1. Priority local parts at org domain
        for pref in ("info", "contact", "admin", "office", "hello", "mail", "director", "ed"):
            for e in org_emails:
                if e.startswith(pref + "@"):
                    return e

        # 2. Any org domain email
        if org_emails:
            return org_emails[0]

        # 3. Priority local parts at any domain
        for pref in ("info", "contact", "admin", "office", "hello", "mail", "director", "ed"):
            for e in priority_emails:
                if e.startswith(pref + "@"):
                    return e

        # 4. Any non-junk email
        if other_emails:
            return other_emails[0]

        return None

    def _extract_contact_name(self, text: str) -> Optional[str]:
        """Extract a contact person's name using title patterns."""
        for match in TITLE_RE.finditer(text):
            # Groups: 1=Title: Name, 2=Name, Title, 3=Name (Title)
            name = match.group(1) or match.group(2) or match.group(3)
            if name:
                name = name.strip()
                # Filter out common false positives
                if len(name) < 5 or len(name) > 50:
                    continue
                if name.lower() in ("lorem ipsum", "jane doe", "john doe"):
                    continue
                return name
        return None

    def __del__(self):
        try:
            self._client.close()
        except Exception:
            pass