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
