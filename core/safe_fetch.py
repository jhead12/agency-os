"""
Fetching customer-supplied URLs from our server (docs/U9ITUS_BILLING.md, task B7).

RSS and scrape searches fetch pages a customer named, so every fetch goes
through SafeFetcher:

- DNS is resolved here, and the request goes to the checked IP (the original
  host is sent as Host and TLS SNI), so a DNS answer that changes between the
  check and the connect can't reach an internal address.
- Private, loopback, link-local, CGNAT, multicast, reserved and cloud metadata
  addresses are refused, on every redirect hop (redirects are followed here,
  at most MAX_REDIRECTS).
- http/https on ports 80 and 443 only; hosts in AGENCY_OS_FETCH_BLOCKLIST are refused.
- robots.txt is honored, at most one request per second per host, and a page
  may be at most MAX_BYTES and take at most TIMEOUT_SECONDS.

Problems raise FetchRefused, a ValueError whose message is safe to show the customer.
"""

from __future__ import annotations

import ipaddress
import os
import socket
import time
from dataclasses import dataclass
from typing import Callable, Optional
from urllib.parse import urljoin, urlsplit, urlunsplit
from urllib.robotparser import RobotFileParser

import httpx

MAX_BYTES = 2 * 1024 * 1024
TIMEOUT_SECONDS = 15
MAX_REDIRECTS = 5
MIN_INTERVAL_SECONDS = 1.0
PORTS = {"http": 80, "https": 443}
USER_AGENT = "agency-os prospect search (+https://u9itus.com)"
_CGNAT = ipaddress.ip_network("100.64.0.0/10")
_METADATA = {ipaddress.ip_address("169.254.169.254"), ipaddress.ip_address("fd00:ec2::254")}


class FetchRefused(ValueError):
    """A URL we won't fetch, or a page we couldn't read; the message says why."""


@dataclass
class Page:
    url: str            # the final URL, with its real host name
    status_code: int
    headers: httpx.Headers
    content: bytes

    @property
    def text(self) -> str:
        for encoding in filter(None, [self._charset(), "utf-8"]):
            try:
                return self.content.decode(encoding)
            except (LookupError, UnicodeDecodeError):
                continue
        return self.content.decode("utf-8", errors="replace")

    def _charset(self) -> Optional[str]:
        for part in self.headers.get("content-type", "").split(";")[1:]:
            key, _, value = part.strip().partition("=")
            if key.lower() == "charset" and value:
                return value.strip('"\' ')
        return None


def allowed_ip(ip: ipaddress._BaseAddress) -> bool:
    """Whether an address is on the public internet."""
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return not (ip in _METADATA or ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast
                or ip.is_reserved or ip.is_unspecified or (ip.version == 4 and ip in _CGNAT))


def blocklist() -> set[str]:
    raw = os.environ.get("AGENCY_OS_FETCH_BLOCKLIST", "")
    return {h.strip().lower().lstrip(".") for h in raw.split(",") if h.strip()}


def same_host(a: str, b: str) -> bool:
    return (urlsplit(a).hostname or "").lower() == (urlsplit(b).hostname or "").lower()


class SafeFetcher:
    """Guarded GETs for one search run. Not thread-safe; make one per run."""

    def __init__(self, transport: Optional[httpx.BaseTransport] = None,
                 resolve: Optional[Callable] = None,
                 clock: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep,
                 user_agent: str = USER_AGENT):
        self._client = httpx.Client(transport=transport, timeout=TIMEOUT_SECONDS, follow_redirects=False,
                                    headers={"User-Agent": user_agent})
        self._resolve = resolve or (lambda *a, **kw: socket.getaddrinfo(*a, **kw))
        self._clock, self._sleep = clock, sleep
        self._user_agent = user_agent
        self._last: dict[str, float] = {}
        self._robots: dict[str, RobotFileParser] = {}
        self.requests = 0  # every request made, robots.txt included

    def close(self) -> None:
        self._client.close()

    # ── Checks ────────────────────────────────────────────────────────

    def check_url(self, url: str) -> tuple[str, str, int]:
        """(scheme, host, port) of a URL we may fetch, or FetchRefused."""
        try:
            parts = urlsplit(url)
            port = parts.port
        except ValueError:
            raise FetchRefused("That isn't a valid web address.") from None
        scheme, host = parts.scheme.lower(), (parts.hostname or "").lower()
        if scheme not in PORTS or not host:
            raise FetchRefused("Only http:// and https:// web addresses can be fetched.")
        port = port or PORTS[scheme]
        if port != PORTS[scheme]:
            raise FetchRefused("Only the standard web ports (80 and 443) can be fetched.")
        if parts.username or parts.password:
            raise FetchRefused("Web addresses with a user name or password can't be fetched.")
        if any(host == b or host.endswith("." + b) for b in blocklist()):
            raise FetchRefused(f"{host} is on the do-not-fetch list.")
        return scheme, host, port

    def _address(self, host: str, port: int) -> str:
        """A checked public IP for host. Every address it resolves to must be public."""
        try:
            literal = ipaddress.ip_address(host.strip("[]"))
            addresses = [literal]
        except ValueError:
            try:
                infos = self._resolve(host, port, proto=socket.IPPROTO_TCP)
            except (socket.gaierror, UnicodeError, OSError):
                raise FetchRefused(f"Couldn't find the site {host}.") from None
            addresses = []
            for info in infos:
                try:
                    addresses.append(ipaddress.ip_address(info[4][0].split("%")[0]))
                except ValueError:
                    continue
        if not addresses:
            raise FetchRefused(f"Couldn't find the site {host}.")
        if not all(allowed_ip(a) for a in addresses):
            raise FetchRefused(f"{host} points to a private or internal address, which can't be fetched.")
        ip = addresses[0]
        return f"[{ip}]" if ip.version == 6 else str(ip)

    def _pace(self, host: str) -> None:
        last = self._last.get(host)
        if last is not None:
            wait = MIN_INTERVAL_SECONDS - (self._clock() - last)
            if wait > 0:
                self._sleep(wait)
        self._last[host] = self._clock()

    # ── Fetching ──────────────────────────────────────────────────────

    def _one(self, url: str) -> Page:
        """One GET to the checked IP, no redirects followed."""
        scheme, host, port = self.check_url(url)
        address = self._address(host, port)
        parts = urlsplit(url)
        netloc = address if port == PORTS[scheme] else f"{address}:{port}"
        target = urlunsplit((scheme, netloc, parts.path or "/", parts.query, ""))
        self._pace(host)
        self.requests += 1
        extensions = {"sni_hostname": host} if scheme == "https" else {}
        try:
            with self._client.stream("GET", target, headers={"Host": parts.netloc.rsplit("@", 1)[-1]},
                                     extensions=extensions) as response:
                if int(response.headers.get("content-length") or 0) > MAX_BYTES:
                    raise FetchRefused(f"{host} sent a page larger than 2 MB.")
                body = bytearray()
                for chunk in response.iter_bytes():
                    body.extend(chunk)
                    if len(body) > MAX_BYTES:
                        raise FetchRefused(f"{host} sent a page larger than 2 MB.")
                return Page(url, response.status_code, response.headers, bytes(body))
        except httpx.TimeoutException:
            raise FetchRefused(f"{host} took too long to answer.") from None
        except httpx.HTTPError:
            raise FetchRefused(f"Couldn't connect to {host}.") from None

    def _follow(self, url: str) -> Page:
        for _ in range(MAX_REDIRECTS + 1):
            page = self._one(url)
            location = page.headers.get("location")
            if page.status_code in (301, 302, 303, 307, 308) and location:
                url = urljoin(url, location)
                continue
            return page
        raise FetchRefused("The site redirected too many times.")

    def robots_allows(self, url: str) -> bool:
        scheme, host, port = self.check_url(url)
        key = f"{scheme}://{host}"
        parser = self._robots.get(key)
        if parser is None:
            parser = RobotFileParser()
            try:
                page = self._follow(f"{key}/robots.txt")
            except FetchRefused:
                page = None
            if page is not None and page.status_code in (401, 403):
                parser.disallow_all = True
            elif page is not None and page.status_code == 200:
                parser.parse(page.text.splitlines())
            else:
                parser.allow_all = True
            self._robots[key] = parser
        return parser.can_fetch(self._user_agent, url)

    def get(self, url: str) -> Page:
        """Fetch a page the customer named. Raises FetchRefused; a non-2xx answer is also refused."""
        if not self.robots_allows(url):
            raise FetchRefused(f"{urlsplit(url).hostname}'s robots.txt doesn't allow fetching that page.")
        page = self._follow(url)
        if page.url != url and not self.robots_allows(page.url):
            raise FetchRefused(f"{urlsplit(page.url).hostname}'s robots.txt doesn't allow fetching that page.")
        if not 200 <= page.status_code < 300:
            raise FetchRefused(f"{urlsplit(page.url).hostname} answered with an error ({page.status_code}).")
        return page
