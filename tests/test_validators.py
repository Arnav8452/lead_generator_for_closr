"""
Closr — Validator Unit Tests
Tests every validation gate: generic names, confidence threshold,
URL detection, icebreaker platitudes, enterprise blocklist, etc.
"""

import json
import os
import pytest

# Ensure config doesn't try to validate on import during tests
os.environ.setdefault("SUPABASE_URL", "https://test.supabase.co")
os.environ.setdefault("SUPABASE_KEY", "test_key")
os.environ.setdefault("OLLAMA_BASE_URL", "http://localhost:11434")
os.environ.setdefault("REDDIT_CLIENT_ID", "test_id")
os.environ.setdefault("REDDIT_CLIENT_SECRET", "test_secret")
os.environ.setdefault("HUNTER_API_KEY", "test_hunter")

from pipeline.validator import validate_lead


# ─────────────────────────────────────────────────────────
# Load test fixtures
# ─────────────────────────────────────────────────────────
FIXTURES_DIR = os.path.join(os.path.dirname(__file__), "fixtures")


def load_fixtures():
    with open(os.path.join(FIXTURES_DIR, "mock_leads.json"), "r") as f:
        return json.load(f)


# ─────────────────────────────────────────────────────────
# Valid Lead Tests
# ─────────────────────────────────────────────────────────
class TestValidLeads:
    """Test that well-formed leads pass all validation gates."""

    def test_valid_lead_passes(self):
        """A lead with proper brand name, high confidence, and specific icebreaker should pass."""
        lead = {
            "brand_name": "GlowUp Skincare",
            "confidence": 0.92,
            "icebreaker_pitch": "Saw your Series A announcement — creator partnerships could 3x your ROAS.",
        }
        is_valid, reason = validate_lead(lead)
        assert is_valid is True
        assert reason == "ok"

    def test_minimum_confidence_passes(self):
        """A lead at exactly the confidence threshold should pass."""
        lead = {
            "brand_name": "TestBrand",
            "confidence": 0.6,  # Exactly at threshold
            "icebreaker_pitch": "Your recent hiring push for UGC roles caught my attention.",
        }
        is_valid, reason = validate_lead(lead)
        assert is_valid is True

    def test_no_icebreaker_still_passes(self):
        """Leads without an icebreaker should still pass (icebreaker is optional)."""
        lead = {
            "brand_name": "SomeBrand",
            "confidence": 0.8,
        }
        is_valid, reason = validate_lead(lead)
        assert is_valid is True


# ─────────────────────────────────────────────────────────
# Invalid Lead Tests
# ─────────────────────────────────────────────────────────
class TestInvalidLeads:
    """Test that malformed or suspicious leads are correctly rejected."""

    def test_empty_brand_name_rejected(self):
        """Empty brand name should be rejected."""
        lead = {"brand_name": "", "confidence": 0.9}
        is_valid, reason = validate_lead(lead)
        assert is_valid is False
        assert "missing_brand_name" in reason

    def test_missing_brand_name_rejected(self):
        """Missing brand_name key should be rejected."""
        lead = {"confidence": 0.9}
        is_valid, reason = validate_lead(lead)
        assert is_valid is False
        assert "missing_brand_name" in reason

    def test_generic_brand_name_rejected(self):
        """Generic LLM-hallucinated brand names should be rejected."""
        for name in ["company", "brand", "startup", "unknown", "n/a"]:
            lead = {"brand_name": name, "confidence": 0.9}
            is_valid, reason = validate_lead(lead)
            assert is_valid is False, f"'{name}' should be rejected"
            assert "generic_brand_name" in reason

    def test_low_confidence_rejected(self):
        """Leads below the confidence threshold should be rejected."""
        lead = {"brand_name": "TestBrand", "confidence": 0.3}
        is_valid, reason = validate_lead(lead)
        assert is_valid is False
        assert "low_confidence" in reason

    def test_url_brand_name_rejected(self):
        """Brand names that look like URLs should be rejected."""
        lead = {
            "brand_name": "https://example.com",
            "confidence": 0.9,
        }
        is_valid, reason = validate_lead(lead)
        assert is_valid is False
        assert "brand_is_url" in reason

    def test_long_brand_name_rejected(self):
        """Overly long brand names should be rejected."""
        lead = {
            "brand_name": "A" * 50,  # Exceeds MAX_BRAND_NAME_LENGTH (40)
            "confidence": 0.9,
        }
        is_valid, reason = validate_lead(lead)
        assert is_valid is False
        assert "brand_name_too_long" in reason

    def test_generic_icebreaker_rejected(self):
        """Icebreakers containing generic platitudes should be rejected."""
        lead = {
            "brand_name": "TestBrand",
            "confidence": 0.9,
            "icebreaker_pitch": "Hope this email finds you well! I'd love to work together.",
        }
        is_valid, reason = validate_lead(lead)
        assert is_valid is False
        assert "generic_icebreaker" in reason

    def test_enterprise_blocklist_rejected(self):
        """Enterprise brands on the blocklist should be rejected."""
        for name in ["Google", "Meta AI", "Amazon Fresh", "Nike Sports"]:
            lead = {"brand_name": name, "confidence": 0.95}
            is_valid, reason = validate_lead(lead)
            assert is_valid is False, f"'{name}' should be blocked"
            assert "enterprise_blocklist" in reason

    def test_numeric_brand_name_rejected(self):
        """Purely numeric brand names should be rejected."""
        lead = {"brand_name": "12345", "confidence": 0.9}
        is_valid, reason = validate_lead(lead)
        assert is_valid is False
        assert "numeric_brand_name" in reason


# ─────────────────────────────────────────────────────────
# Fixture-Based Tests
# ─────────────────────────────────────────────────────────
class TestFixtureLeads:
    """Test validation against the full mock_leads.json fixture file."""

    def test_fixture_valid_leads_pass(self):
        """The first 3 fixtures (valid leads) should all pass validation."""
        fixtures = load_fixtures()
        for i in range(3):
            lead = fixtures[i]
            is_valid, reason = validate_lead(lead)
            assert is_valid is True, (
                f"Fixture lead {i} ('{lead['brand_name']}') should pass "
                f"but was rejected: {reason}"
            )

    def test_fixture_invalid_leads_rejected(self):
        """Fixtures 3-6 (invalid leads) should all be rejected."""
        fixtures = load_fixtures()
        for i in range(3, len(fixtures)):
            lead = fixtures[i]
            is_valid, reason = validate_lead(lead)
            assert is_valid is False, (
                f"Fixture lead {i} ('{lead.get('brand_name')}') should be "
                f"rejected but passed"
            )
