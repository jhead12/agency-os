"""Tests for enricher conditional configuration (Apollo free-plan, Hunter key
validation) and local_scraper's mailto:/tel: fallback.

No network, no Postgres — HTTP responses are mocked.
"""

from __future__ import annotations

import re
from unittest.mock import MagicMock, patch

import pytest

from core.models import Prospect, EnrichmentResult


def _make_apollo():
    from plugins.enrichers.apollo import ApolloEnricher
    return ApolloEnricher()


def _make_hunter():
    from plugins.enrichers.hunter import HunterEnricher
    return HunterEnricher()


def _make_scraper():
    from plugins.enrichers.local_scraper import LocalScraperEnricher
    return LocalScraperEnricher()


# ── Apollo free-plan aware is_configured ──────────────────────────────


def test_apollo_is_configured_false_without_key(monkeypatch):
    monkeypatch.delenv("APOLLO_API_KEY", raising=False)
    assert _make_apollo().is_configured() is False


def test_apollo_is_configured_true_with_paid_key(monkeypatch):
    monkeypatch.setenv("APOLLO_API_KEY", "valid-paid-key")
    enricher = _make_apollo()
    # Simulate successful plan check
    enricher._plan_accessible = True
    assert enricher.is_configured() is True


def test_apollo_is_configured_false_on_free_plan(monkeypatch):
    """A free-plan key must make is_configured() False so the pipeline skips it."""
    monkeypatch.setenv("APOLLO_API_KEY", "free-plan-key")
    enricher = _make_apollo()

    mock_resp = MagicMock()
    mock_resp.status_code = 403
    with patch.object(enricher._client, "post", return_value=mock_resp):
        assert enricher.is_configured() is False
    # Cached: second call doesn't hit the network again
    with patch.object(enricher._client, "post", side_effect=AssertionError("should not be called")):
        assert enricher.is_configured() is False


def test_apollo_is_configured_caches_success(monkeypatch):
    monkeypatch.setenv("APOLLO_API_KEY", "paid-key")
    enricher = _make_apollo()
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    with patch.object(enricher._client, "post", return_value=mock_resp) as m:
        assert enricher.is_configured() is True
        assert enricher.is_configured() is True
        assert m.call_count == 1


# ── Hunter key-validated is_configured ────────────────────────────────


def test_hunter_is_configured_false_without_key(monkeypatch):
    monkeypatch.delenv("HUNTER_API_KEY", raising=False)
    assert _make_hunter().is_configured() is False


def test_hunter_is_configured_true_with_valid_key(monkeypatch):
    monkeypatch.setenv("HUNTER_API_KEY", "valid-key")
    enricher = _make_hunter()
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    with patch("plugins.enrichers.hunter.httpx.get", return_value=mock_resp):
        assert enricher.is_configured() is True


def test_hunter_is_configured_false_with_invalid_key(monkeypatch):
    monkeypatch.setenv("HUNTER_API_KEY", "bad-key")
    enricher = _make_hunter()
    mock_resp = MagicMock()
    mock_resp.status_code = 401
    with patch("plugins.enrichers.hunter.httpx.get", return_value=mock_resp):
        assert enricher.is_configured() is False


def test_hunter_is_configured_caches_validation(monkeypatch):
    monkeypatch.setenv("HUNTER_API_KEY", "valid-key")
    enricher = _make_hunter()
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    with patch("plugins.enrichers.hunter.httpx.get", return_value=mock_resp) as m:
        assert enricher.is_configured() is True
        assert enricher.is_configured() is True
        assert m.call_count == 1


# ── local_scraper mailto:/tel: fallback ───────────────────────────────

def _prospect(name="Test Org", website="https://example.org"):
    return Prospect(name=name, website_url=website)


def _scraper_with_pages(pages_html: list[str]):
    """A LocalScraperEnricher whose _find_website/_fetch_pages are stubbed."""
    scraper = _make_scraper()
    scraper._find_website = lambda prospect: "https://example.org"
    scraper._fetch_pages = lambda base_url: [
        (f"https://example.org/{i}", html) for i, html in enumerate(pages_html)
    ]
    return scraper


def test_scraper_extracts_mailto_email_from_html():
    """mailto: links in raw HTML yield an email when visible text has none."""
    scraper = _scraper_with_pages([
        # No visible text email, but a mailto link in a JS-rendered button
        '<html><body><p>No email in text</p><a href="mailto:contact@example.org">Email us</a></body></html>'
    ])
    result = scraper.enrich(_prospect())
    assert result.contact_email == "contact@example.org"


def test_scraper_extracts_tel_phone_from_html():
    """tel: links in raw HTML yield a phone when visible text has none."""
    scraper = _scraper_with_pages([
        '<html><body><p>Call us any time</p><a href="tel:+12134850214">Call</a></body></html>'
    ])
    result = scraper.enrich(_prospect())
    assert result.contact_phone == "(213) 485-0214"


def test_scraper_prefers_visible_text_over_mailto():
    """Visible-text extraction still wins when both exist."""
    scraper = _scraper_with_pages([
        '<html><body><p>Reach us at info@example.org</p>'
        '<a href="mailto:other@example.org">Email</a></body></html>'
    ])
    result = scraper.enrich(_prospect())
    assert result.contact_email == "info@example.org"


def test_scraper_mailto_filters_junk_domains():
    """mailto: fallback ignores junk domains the same way visible-text does."""
    scraper = _scraper_with_pages([
        '<html><body><a href="mailto:noreply@sentry.io">Sentry</a>'
        '<a href="mailto:real@example.org">Real</a></body></html>'
    ])
    result = scraper.enrich(_prospect())
    assert result.contact_email == "real@example.org"


# ── local_scraper site summary ────────────────────────────────────────

ABOUT = ("We are a neighborhood nonprofit that registers first-time voters in East Los Angeles "
         "and runs a youth civic leadership program every summer.")


def test_scraper_summary_prefers_about_page():
    scraper = _make_scraper()
    scraper._find_website = lambda prospect: "https://example.org"
    scraper._fetch_pages = lambda base_url: [
        ("https://example.org", '<html><head><meta name="description" content="Homepage blurb about '
                                'our organization and what we do."></head><body></body></html>'),
        ("https://example.org/about", f"<html><body><p>Short.</p><p>{ABOUT}</p>"
                                      "<p>© 2026 Example Org. All rights reserved and then some more words.</p></body></html>"),
    ]
    assert scraper.enrich(_prospect()).raw["site_summary"] == ABOUT


def test_scraper_summary_falls_back_to_meta_description():
    scraper = _scraper_with_pages([
        '<html><head><meta property="og:description" content="Free legal clinics for tenants across '
        'South LA since 1998."></head><body><p>Hi</p></body></html>'
    ])
    assert scraper.enrich(_prospect()).raw["site_summary"] == "Free legal clinics for tenants across South LA since 1998."


def test_scraper_summary_is_clipped():
    scraper = _scraper_with_pages([f"<html><body>{f'<p>{ABOUT}</p>' * 10}</body></html>"])
    summary = scraper.enrich(_prospect()).raw["site_summary"]
    assert len(summary) <= 500 and summary.endswith(".")


def test_scraper_no_summary_without_text():
    scraper = _scraper_with_pages(["<html><body><p>Contact us</p></body></html>"])
    assert "site_summary" not in scraper.enrich(_prospect()).raw


# ── Firecrawl ─────────────────────────────────────────────────────────


def _make_firecrawl(monkeypatch, key="fc-test"):
    from plugins.enrichers.firecrawl import FirecrawlEnricher
    if key:
        monkeypatch.setenv("FIRECRAWL_API_KEY", key)
    else:
        monkeypatch.delenv("FIRECRAWL_API_KEY", raising=False)
    enricher = FirecrawlEnricher()
    if key:
        enricher._key_valid = True
    return enricher


def _resp(payload, status=200):
    resp = MagicMock()
    resp.status_code = status
    resp.json.return_value = payload
    return resp


def _scrape_resp(fields, links=None):
    return _resp({"success": True, "data": {"json": fields, "links": links or []}})


def test_firecrawl_is_configured_false_without_key(monkeypatch):
    assert _make_firecrawl(monkeypatch, key=None).is_configured() is False


def test_firecrawl_is_configured_probes_once(monkeypatch):
    enricher = _make_firecrawl(monkeypatch)
    enricher._key_valid = None
    with patch.object(enricher._client, "get", return_value=_resp({}, 401)) as m:
        assert enricher.is_configured() is False
        assert enricher.is_configured() is False
        assert m.call_count == 1


def test_firecrawl_extracts_and_normalizes(monkeypatch):
    enricher = _make_firecrawl(monkeypatch)
    fields = {
        "contact_name": "Jane Smith",
        "contact_title": "Managing Partner",
        "contact_email": "Jane@SmithLaw.com",
        "contact_phone": "+1 213.627.0142",
        "summary": "A civil litigation firm in Los Angeles.",
    }
    with patch.object(enricher._client, "post", return_value=_scrape_resp(fields)) as m:
        result = enricher.enrich(_prospect(website="smithlaw.com"))
    assert m.call_count == 1  # email + phone on the homepage: no second scrape
    assert result.raw["website"] == "https://smithlaw.com"
    assert result.contact_email == "jane@smithlaw.com"
    assert result.contact_phone == "(213) 627-0142"
    assert result.contact_name == "Jane Smith"
    assert result.contact_title == "Managing Partner"
    assert result.raw["site_summary"] == "A civil litigation firm in Los Angeles."
    assert result.confidence == pytest.approx(1.0)


def test_firecrawl_scrapes_contact_page_when_missing(monkeypatch):
    enricher = _make_firecrawl(monkeypatch)
    home = _scrape_resp(
        {"contact_name": "Jane Smith"},
        links=["https://www.example.org/blog", "https://example.org/contact-us",
               "https://facebook.com/example"],
    )
    contact = _scrape_resp({"contact_email": "info@example.org", "contact_name": "Someone Else"})
    with patch.object(enricher._client, "post", side_effect=[home, contact]) as m:
        result = enricher.enrich(_prospect())
    assert m.call_args_list[1].kwargs["json"]["url"] == "https://example.org/contact-us"
    assert result.contact_email == "info@example.org"
    assert result.contact_name == "Jane Smith"  # homepage value kept


def test_firecrawl_drops_junk_email_and_toll_free(monkeypatch):
    enricher = _make_firecrawl(monkeypatch)
    fields = {"contact_email": "someone@gmail.com", "contact_phone": "(800) 555-1212"}
    with patch.object(enricher._client, "post", return_value=_scrape_resp(fields)):
        result = enricher.enrich(_prospect())
    assert result.contact_email is None
    assert result.contact_phone is None


def test_firecrawl_searches_when_no_website(monkeypatch):
    enricher = _make_firecrawl(monkeypatch)
    search = _resp({"success": True, "data": {"web": [
        {"url": "https://www.yelp.com/biz/test-org"},
        {"url": "https://testorg.com/"},
    ]}})
    scrape = _scrape_resp({"contact_email": "info@testorg.com", "contact_phone": "213-627-0100"})
    with patch.object(enricher._client, "post", side_effect=[search, scrape]):
        result = enricher.enrich(Prospect(name="Test Org", city="Pasadena", state="CA"))
    assert result.raw["website"] == "https://testorg.com/"
    assert result.contact_email == "info@testorg.com"


def test_firecrawl_never_raises(monkeypatch):
    enricher = _make_firecrawl(monkeypatch)
    with patch.object(enricher._client, "post", side_effect=httpx_error()):
        result = enricher.enrich(_prospect())
    assert result.source == "firecrawl"
    assert result.contact_email is None


def httpx_error():
    import httpx
    return httpx.ConnectError("boom")
