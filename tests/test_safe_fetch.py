"""
The guarded fetcher for customer-supplied URLs (core/safe_fetch.py, task B7).
DNS is faked and HTTP goes through httpx.MockTransport; nothing touches the network.

Run: python -m pytest tests/test_safe_fetch.py
"""

import socket
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import safe_fetch  # noqa: E402
from core.safe_fetch import FetchRefused, SafeFetcher  # noqa: E402

PUBLIC = "93.184.216.34"


def resolver(table: dict):
    """getaddrinfo from {host: ip or [ip for each call]}."""
    calls: dict = {}

    def resolve(host, port, **_):
        answer = table.get(host)
        if answer is None:
            raise socket.gaierror("not found")
        if isinstance(answer, list):
            n = calls.get(host, 0)
            calls[host] = n + 1
            answer = answer[min(n, len(answer) - 1)]
        family = socket.AF_INET6 if ":" in answer else socket.AF_INET
        return [(family, socket.SOCK_STREAM, 6, "", (answer, port))]
    return resolve


def site(pages: dict, seen: list | None = None):
    """A transport serving {(host, path): response or (status, body, headers)}; 404 otherwise."""
    def handler(request: httpx.Request):
        if seen is not None:
            seen.append((request.url.host, request.headers["host"], request.url.path))
        found = pages.get((request.headers["host"], request.url.path))
        if found is None:
            return httpx.Response(404)
        if isinstance(found, httpx.Response):
            return found
        status, body, headers = found
        return httpx.Response(status, content=body, headers=headers)
    return httpx.MockTransport(handler)


def fetcher(pages, table, **kw):
    return SafeFetcher(transport=site(pages, kw.pop("seen", None)), resolve=resolver(table),
                       clock=kw.pop("clock", lambda: 0.0), sleep=kw.pop("sleep", lambda s: None), **kw)


@pytest.mark.parametrize("ip", ["10.0.0.5", "127.0.0.1", "169.254.169.254", "100.64.1.1", "192.168.1.1",
                                "0.0.0.0", "::1", "fd00::1", "fd00:ec2::254", "::ffff:10.0.0.1", "224.0.0.1"])
def test_internal_addresses_are_refused(ip):
    f = fetcher({("intranet.test", "/"): (200, b"secret", {})}, {"intranet.test": ip})
    with pytest.raises(FetchRefused, match="private or internal"):
        f.get("http://intranet.test/")


def test_ip_literals_schemes_ports_and_credentials_are_checked():
    f = fetcher({}, {})
    for url, why in [("http://127.0.0.1/", "private or internal"), ("http://[::1]/", "private or internal"),
                     ("ftp://example.org/", "Only http"), ("file:///etc/passwd", "Only http"),
                     ("http://example.org:8080/", "standard web ports"),
                     ("http://user:pw@example.org/", "user name or password")]:
        with pytest.raises(FetchRefused, match=why):
            f.get(url)


def test_the_request_goes_to_the_checked_ip_with_the_real_host_name():
    seen = []
    f = fetcher({("example.org", "/list"): (200, b"<html>ok</html>", {})}, {"example.org": PUBLIC}, seen=seen)
    page = f.get("http://example.org/list")
    assert page.text == "<html>ok</html>" and page.url == "http://example.org/list"
    assert (PUBLIC, "example.org", "/list") in seen


def test_a_redirect_to_an_internal_address_is_refused():
    pages = {("example.org", "/go"): (302, b"", {"location": "http://metadata.test/latest/meta-data/"}),
             ("metadata.test", "/latest/meta-data/"): (200, b"keys", {})}
    f = fetcher(pages, {"example.org": PUBLIC, "metadata.test": "169.254.169.254"})
    with pytest.raises(FetchRefused, match="private or internal"):
        f.get("http://example.org/go")


def test_dns_that_changes_after_the_first_check_is_checked_again():
    # Public for robots.txt, then internal: the page request must be refused, not sent.
    seen = []
    f = fetcher({("rebind.test", "/robots.txt"): (404, b"", {}), ("rebind.test", "/"): (200, b"x", {})},
                {"rebind.test": [PUBLIC, "10.0.0.7"]}, seen=seen)
    with pytest.raises(FetchRefused, match="private or internal"):
        f.get("http://rebind.test/")
    assert [p for _, _, p in seen] == ["/robots.txt"]


def test_a_host_resolving_to_any_internal_address_is_refused(monkeypatch):
    def both(host, port, **_):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (PUBLIC, port)),
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.1.1.1", port))]
    f = SafeFetcher(transport=site({}), resolve=both, sleep=lambda s: None)
    with pytest.raises(FetchRefused, match="private or internal"):
        f.get("http://mixed.test/")


def test_robots_txt_is_honored():
    robots = b"User-agent: *\nDisallow: /private\n"
    pages = {("example.org", "/robots.txt"): (200, robots, {}), ("example.org", "/ok"): (200, b"fine", {}),
             ("example.org", "/private/list"): (200, b"no", {})}
    f = fetcher(pages, {"example.org": PUBLIC})
    assert f.get("http://example.org/ok").text == "fine"
    with pytest.raises(FetchRefused, match="robots.txt"):
        f.get("http://example.org/private/list")
    assert f.requests == 2  # robots.txt once, then the allowed page; the refused page isn't requested


def test_pages_over_two_megabytes_are_refused():
    big = b"x" * (safe_fetch.MAX_BYTES + 1)
    f = fetcher({("example.org", "/big"): httpx.Response(200, stream=httpx.ByteStream(big))},
                {"example.org": PUBLIC})
    with pytest.raises(FetchRefused, match="larger than 2 MB"):
        f.get("http://example.org/big")


def test_at_most_one_request_per_second_per_host():
    now, sleeps = [100.0], []

    def sleep(s):
        sleeps.append(round(s, 2))
        now[0] += s
    pages = {("example.org", "/a"): (200, b"a", {}), ("example.org", "/b"): (200, b"b", {})}
    f = fetcher(pages, {"example.org": PUBLIC}, clock=lambda: now[0], sleep=sleep)
    f.get("http://example.org/a")
    f.get("http://example.org/b")
    assert sleeps == [1.0, 1.0]  # robots.txt, then /a after 1 s, then /b after 1 s


def test_blocklisted_hosts_and_error_answers_are_refused(monkeypatch):
    monkeypatch.setenv("AGENCY_OS_FETCH_BLOCKLIST", "linkedin.com, .facebook.com")
    f = fetcher({("example.org", "/gone"): (410, b"", {})}, {"example.org": PUBLIC})
    with pytest.raises(FetchRefused, match="do-not-fetch"):
        f.get("https://www.linkedin.com/company/x")
    with pytest.raises(FetchRefused, match=r"error \(410\)"):
        f.get("http://example.org/gone")
