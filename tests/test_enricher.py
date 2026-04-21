"""
Closr — Enricher Unit Tests
Tests the LayeredEnricher orchestration logic, Clearbit domain resolution,
and credit-budget sorting. Uses mocked API responses.
"""

import os
import pytest
from unittest.mock import patch, MagicMock

# Ensure config doesn't try to validate on import during tests
os.environ.setdefault("SUPABASE_URL", "https://test.supabase.co")
os.environ.setdefault("SUPABASE_KEY", "test_key")
os.environ.setdefault("OLLAMA_BASE_URL", "http://localhost:11434")
os.environ.setdefault("REDDIT_CLIENT_ID", "test_id")
os.environ.setdefault("REDDIT_CLIENT_SECRET", "test_secret")
os.environ.setdefault("HUNTER_API_KEY", "test_hunter")
os.environ.setdefault("SNOV_CLIENT_ID", "test_snov")
os.environ.setdefault("PROSPEO_API_KEY", "test_prospeo")

from enrichment.clearbit import resolve_domain
from enrichment.enricher import LayeredEnricher


# ─────────────────────────────────────────────────────────
# Clearbit Domain Resolution Tests
# ─────────────────────────────────────────────────────────
class TestClearbitResolver:
    """Test Clearbit autocomplete domain resolution."""

    @patch("enrichment.clearbit.requests.get")
    def test_exact_match(self, mock_get):
        """An exact brand name match should return the domain."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = [
            {"name": "Glossier", "domain": "glossier.com"},
        ]
        mock_response.raise_for_status = MagicMock()
        mock_get.return_value = mock_response

        # Clear the cache for this test
        resolve_domain.__defaults__[0].clear()

        result = resolve_domain("Glossier")
        assert result == "glossier.com"

    @patch("enrichment.clearbit.requests.get")
    def test_fuzzy_match_above_threshold(self, mock_get):
        """A fuzzy match above the threshold should return the domain."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = [
            {"name": "Glossier Inc", "domain": "glossier.com"},
        ]
        mock_response.raise_for_status = MagicMock()
        mock_get.return_value = mock_response

        resolve_domain.__defaults__[0].clear()

        result = resolve_domain("Glossier")
        assert result == "glossier.com"

    @patch("enrichment.clearbit.requests.get")
    def test_no_suggestions(self, mock_get):
        """No suggestions should return None."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = []
        mock_response.raise_for_status = MagicMock()
        mock_get.return_value = mock_response

        resolve_domain.__defaults__[0].clear()

        result = resolve_domain("NonexistentBrand12345")
        assert result is None

    @patch("enrichment.clearbit.requests.get")
    def test_cache_hit(self, mock_get):
        """Second call for same brand should use cache, not API."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = [
            {"name": "CacheBrand", "domain": "cachebrand.com"},
        ]
        mock_response.raise_for_status = MagicMock()
        mock_get.return_value = mock_response

        resolve_domain.__defaults__[0].clear()

        # First call — hits API
        result1 = resolve_domain("CacheBrand")
        assert result1 == "cachebrand.com"
        assert mock_get.call_count == 1

        # Second call — should use cache
        result2 = resolve_domain("CacheBrand")
        assert result2 == "cachebrand.com"
        assert mock_get.call_count == 1  # No additional API call

    @patch("enrichment.clearbit.requests.get")
    def test_api_timeout_returns_none(self, mock_get):
        """API timeout should return None gracefully."""
        import requests
        mock_get.side_effect = requests.exceptions.Timeout("timed out")

        resolve_domain.__defaults__[0].clear()

        result = resolve_domain("TimeoutBrand")
        assert result is None


# ─────────────────────────────────────────────────────────
# LayeredEnricher Queue Building Tests
# ─────────────────────────────────────────────────────────
class TestLayeredEnricherQueue:
    """Test the credit-sorted enricher queue logic."""

    @patch("enrichment.enricher.get_enricher_usage")
    def test_queue_sorted_by_remaining_credits(self, mock_usage):
        """Enrichers should be sorted by most remaining credits first."""
        # Hunter: 25 limit, 20 used = 5 remaining
        # Snov: 50 limit, 10 used = 40 remaining
        # Prospeo: 75 limit, 70 used = 5 remaining
        def side_effect(enricher):
            return {"hunter": 20, "snov": 10, "prospeo": 70}.get(enricher, 0)

        mock_usage.side_effect = side_effect

        enricher = LayeredEnricher()
        queue = enricher._build_enricher_queue()

        # Snov should be first (40 remaining), then hunter/prospeo (5 each)
        assert len(queue) == 3
        assert queue[0][0] == "snov"

    @patch("enrichment.enricher.get_enricher_usage")
    def test_exhausted_enricher_excluded(self, mock_usage):
        """An enricher at its monthly limit should be excluded from the queue."""
        def side_effect(enricher):
            return {
                "hunter": 25,   # At limit
                "snov": 10,
                "prospeo": 75,  # At limit
            }.get(enricher, 0)

        mock_usage.side_effect = side_effect

        enricher = LayeredEnricher()
        queue = enricher._build_enricher_queue()

        # Only snov should remain
        assert len(queue) == 1
        assert queue[0][0] == "snov"

    @patch("enrichment.enricher.get_enricher_usage")
    def test_all_exhausted_returns_empty(self, mock_usage):
        """If all enrichers are exhausted, queue should be empty."""
        mock_usage.return_value = 100  # All over limit

        enricher = LayeredEnricher()
        queue = enricher._build_enricher_queue()

        assert len(queue) == 0


# ─────────────────────────────────────────────────────────
# LayeredEnricher End-to-End Tests
# ─────────────────────────────────────────────────────────
class TestLayeredEnricherE2E:
    """Test the full enrichment waterfall with mocked providers."""

    @patch("enrichment.enricher.resolve_domain")
    @patch("enrichment.enricher.hunter.find_email")
    @patch("enrichment.enricher.get_enricher_usage")
    def test_successful_enrichment(self, mock_usage, mock_hunter, mock_clearbit):
        """Full waterfall: Clearbit → Hunter → email found."""
        mock_clearbit.return_value = "glossier.com"
        mock_hunter.return_value = "sarah@glossier.com"
        mock_usage.return_value = 0  # Plenty of budget

        enricher = LayeredEnricher()
        email, domain = enricher.enrich("Glossier")

        assert email == "sarah@glossier.com"
        assert domain == "glossier.com"

    @patch("enrichment.enricher.resolve_domain")
    def test_no_domain_resolved(self, mock_clearbit):
        """If Clearbit can't resolve the domain, return (None, None)."""
        mock_clearbit.return_value = None

        enricher = LayeredEnricher()
        email, domain = enricher.enrich("FakeCompany12345")

        assert email is None
        assert domain is None

    @patch("enrichment.enricher.resolve_domain")
    @patch("enrichment.enricher.hunter.find_email")
    @patch("enrichment.enricher.snov.find_email")
    @patch("enrichment.enricher.prospeo.find_email")
    @patch("enrichment.enricher.get_enricher_usage")
    def test_waterfall_fallthrough(
        self, mock_usage, mock_prospeo, mock_snov, mock_hunter, mock_clearbit
    ):
        """If Hunter fails, waterfall to Snov, then Prospeo."""
        mock_clearbit.return_value = "example.com"
        mock_hunter.return_value = None  # Hunter fails
        mock_snov.return_value = None    # Snov fails
        mock_prospeo.return_value = "contact@example.com"  # Prospeo succeeds
        mock_usage.return_value = 0

        enricher = LayeredEnricher()
        email, domain = enricher.enrich("ExampleBrand")

        assert email == "contact@example.com"
        assert domain == "example.com"

    @patch("enrichment.enricher.resolve_domain")
    @patch("enrichment.enricher.get_enricher_usage")
    def test_all_enrichers_fail(self, mock_usage, mock_clearbit):
        """If all enrichers fail, return (None, domain)."""
        mock_clearbit.return_value = "example.com"
        mock_usage.return_value = 100  # All over limit

        enricher = LayeredEnricher()
        email, domain = enricher.enrich("ExampleBrand")

        assert email is None
        assert domain == "example.com"
