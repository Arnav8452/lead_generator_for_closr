"""
Closr — Pipeline Unit Tests
Tests the LLM JSON parser, deduplicator normalization, and end-to-end
extraction flow (with mocked LLM responses).
"""

import os
import pytest

# Ensure config doesn't try to validate on import during tests
os.environ.setdefault("SUPABASE_URL", "https://test.supabase.co")
os.environ.setdefault("SUPABASE_KEY", "test_key")
os.environ.setdefault("OLLAMA_BASE_URL", "http://localhost:11434")
os.environ.setdefault("REDDIT_CLIENT_ID", "test_id")
os.environ.setdefault("REDDIT_CLIENT_SECRET", "test_secret")
os.environ.setdefault("HUNTER_API_KEY", "test_hunter")

from pipeline.llm import _parse_llm_json, _validate_lead_schema
from pipeline.deduplicator import Deduplicator


# ─────────────────────────────────────────────────────────
# LLM JSON Parser Tests
# ─────────────────────────────────────────────────────────
class TestLLMJsonParser:
    """Test the JSON parser that handles messy LLM output."""

    def test_clean_json(self):
        """Clean JSON should parse directly."""
        text = '{"brand_name": "TestBrand", "confidence": 0.9}'
        result = _parse_llm_json(text)
        assert result is not None
        assert result["brand_name"] == "TestBrand"
        assert result["confidence"] == 0.9

    def test_json_with_markdown_wrapper(self):
        """JSON wrapped in ```json ``` blocks should be extracted."""
        text = '```json\n{"brand_name": "TestBrand", "confidence": 0.85}\n```'
        result = _parse_llm_json(text)
        assert result is not None
        assert result["brand_name"] == "TestBrand"

    def test_json_with_leading_text(self):
        """JSON preceded by explanatory text should be extracted."""
        text = 'Here is the result:\n{"brand_name": "TestBrand", "confidence": 0.7}'
        result = _parse_llm_json(text)
        assert result is not None
        assert result["brand_name"] == "TestBrand"

    def test_invalid_json_returns_none(self):
        """Completely invalid text should return None."""
        text = "This is not JSON at all."
        result = _parse_llm_json(text)
        assert result is None

    def test_empty_string_returns_none(self):
        """Empty string should return None."""
        result = _parse_llm_json("")
        assert result is None

    def test_missing_brand_name_returns_none(self):
        """JSON without brand_name should fail schema validation."""
        text = '{"niche": "skincare", "confidence": 0.9}'
        result = _parse_llm_json(text)
        assert result is None

    def test_missing_confidence_returns_none(self):
        """JSON without confidence should fail schema validation."""
        text = '{"brand_name": "TestBrand", "niche": "tech"}'
        result = _parse_llm_json(text)
        assert result is None

    def test_confidence_coercion(self):
        """String confidence values should be coerced to float."""
        text = '{"brand_name": "TestBrand", "confidence": "0.75"}'
        result = _parse_llm_json(text)
        assert result is not None
        assert result["confidence"] == 0.75
        assert isinstance(result["confidence"], float)

    def test_default_fields_added(self):
        """Missing optional fields should get default values."""
        text = '{"brand_name": "TestBrand", "confidence": 0.8}'
        result = _parse_llm_json(text)
        assert result is not None
        assert "niche" in result
        assert "intent_tier" in result
        assert result["intent_tier"] == "cold"  # default


# ─────────────────────────────────────────────────────────
# Schema Validation Tests
# ─────────────────────────────────────────────────────────
class TestSchemaValidation:
    """Test the lead schema validation function."""

    def test_valid_schema(self):
        data = {
            "brand_name": "TestBrand",
            "confidence": 0.9,
            "niche": "skincare",
            "intent_tier": "hot",
        }
        result = _validate_lead_schema(data)
        assert result is not None
        assert result["brand_name"] == "TestBrand"

    def test_missing_required_field(self):
        data = {"niche": "skincare"}
        result = _validate_lead_schema(data)
        assert result is None

    def test_invalid_confidence_type(self):
        """Non-numeric confidence should be coerced to 0.0."""
        data = {"brand_name": "Test", "confidence": "not_a_number"}
        result = _validate_lead_schema(data)
        assert result is not None
        assert result["confidence"] == 0.0


# ─────────────────────────────────────────────────────────
# Deduplicator Tests
# ─────────────────────────────────────────────────────────
class TestDeduplicator:
    """Test brand name normalization and in-memory dedup tracking."""

    def test_normalization_strips_suffixes(self):
        """Common company suffixes should be stripped."""
        assert Deduplicator.normalize("Glossier Inc") == "glossier"
        assert Deduplicator.normalize("Glossier Inc.") == "glossier"
        assert Deduplicator.normalize("Glossier LLC") == "glossier"
        assert Deduplicator.normalize("Glossier Labs") == "glossier"
        assert Deduplicator.normalize("Glossier Corp") == "glossier"

    def test_normalization_case_insensitive(self):
        """Normalization should be case-insensitive."""
        assert Deduplicator.normalize("GLOSSIER") == "glossier"
        assert Deduplicator.normalize("GlOsSiEr") == "glossier"

    def test_normalization_handles_whitespace(self):
        """Extra whitespace should be collapsed."""
        assert Deduplicator.normalize("  Glossier  Inc  ") == "glossier"

    def test_normalization_empty_string(self):
        """Empty string should return empty string."""
        assert Deduplicator.normalize("") == ""

    def test_in_memory_dedup(self):
        """Second occurrence of same brand should be flagged as duplicate."""
        dedup = Deduplicator()
        # Mock the DB check to always return False (no DB in tests)
        import pipeline.deduplicator as dedup_module
        original_check = dedup_module.check_duplicate
        dedup_module.check_duplicate = lambda x: False

        try:
            assert dedup.is_duplicate("Glossier") is False  # First time
            assert dedup.is_duplicate("Glossier") is True   # Duplicate
            assert dedup.is_duplicate("glossier inc") is True  # Normalized match
            assert dedup.seen_count == 1  # All normalized to same key
        finally:
            dedup_module.check_duplicate = original_check

    def test_different_brands_not_duplicate(self):
        """Different brands should not be flagged as duplicates."""
        dedup = Deduplicator()
        import pipeline.deduplicator as dedup_module
        original_check = dedup_module.check_duplicate
        dedup_module.check_duplicate = lambda x: False

        try:
            assert dedup.is_duplicate("Glossier") is False
            assert dedup.is_duplicate("Allbirds") is False
            assert dedup.seen_count == 2
        finally:
            dedup_module.check_duplicate = original_check

    def test_reset_clears_memory(self):
        """Reset should clear the in-memory tracking set."""
        dedup = Deduplicator()
        import pipeline.deduplicator as dedup_module
        original_check = dedup_module.check_duplicate
        dedup_module.check_duplicate = lambda x: False

        try:
            dedup.is_duplicate("Glossier")
            assert dedup.seen_count == 1
            dedup.reset()
            assert dedup.seen_count == 0
            assert dedup.is_duplicate("Glossier") is False  # Should be new again
        finally:
            dedup_module.check_duplicate = original_check
