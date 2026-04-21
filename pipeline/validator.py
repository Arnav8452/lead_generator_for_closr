"""
Closr — Lead Validator
Post-LLM sanity checks to reject garbage extractions before they
hit the enrichment cascade (which costs real API credits).

Validation gates:
1. brand_name is not generic/placeholder
2. confidence meets the threshold
3. brand_name is not a raw URL
4. icebreaker doesn't contain generic platitudes
5. brand is not on the enterprise blocklist
6. brand_name length is reasonable
"""

import logging
import re

from config import (
    LEAD_CONFIDENCE_THRESHOLD,
    ENTERPRISE_BLOCKLIST,
    MAX_BRAND_NAME_LENGTH,
)

logger = logging.getLogger("closr.pipeline.validator")

# Generic brand names that LLMs hallucinate
GENERIC_NAMES = {
    "company", "brand", "startup", "business", "corporation",
    "enterprise", "organization", "firm", "agency", "unknown",
    "n/a", "none", "null", "the company", "the brand",
    "test", "example", "sample", "demo",
}

# Generic email platitudes that signal a lazy icebreaker
GENERIC_PLATITUDES = [
    "hope this finds you well",
    "hope this email finds you",
    "i hope this message",
    "reaching out to",
    "just wanted to reach out",
    "i came across your",
    "i noticed your company",
    "dear sir/madam",
    "to whom it may concern",
]

# URL-like patterns — brand names should never be URLs
URL_PATTERN = re.compile(r'https?://|www\.|\.com|\.io|\.co\.', re.IGNORECASE)


def validate_lead(lead: dict) -> tuple[bool, str]:
    """
    Run all validation checks on an LLM-extracted lead.

    Args:
        lead: Dict from the LLM extraction pipeline.

    Returns:
        Tuple of (is_valid: bool, reason: str).
        If valid, reason is "ok". If invalid, reason explains why.
    """
    brand_name = (lead.get("brand_name") or "").strip()
    confidence = lead.get("confidence", 0.0)
    icebreaker = (lead.get("icebreaker_pitch") or "").strip()

    # ── Check 1: brand_name must exist and not be empty ─────
    if not brand_name:
        return False, "missing_brand_name"

    # ── Check 2: brand_name must not be generic ─────────────
    if brand_name.lower() in GENERIC_NAMES:
        logger.debug(f"Rejected generic brand name: '{brand_name}'")
        return False, f"generic_brand_name: {brand_name}"

    # ── Check 3: confidence must meet threshold ─────────────
    if confidence < LEAD_CONFIDENCE_THRESHOLD:
        logger.debug(
            f"Rejected '{brand_name}': confidence {confidence} "
            f"< {LEAD_CONFIDENCE_THRESHOLD}"
        )
        return False, f"low_confidence: {confidence}"

    # ── Check 4: brand_name must not be a URL ───────────────
    if URL_PATTERN.search(brand_name):
        logger.debug(f"Rejected URL-like brand name: '{brand_name}'")
        return False, f"brand_is_url: {brand_name}"

    # ── Check 5: brand_name length must be reasonable ───────
    if len(brand_name) > MAX_BRAND_NAME_LENGTH:
        logger.debug(
            f"Rejected overly long brand name ({len(brand_name)} chars): "
            f"'{brand_name[:50]}...'"
        )
        return False, f"brand_name_too_long: {len(brand_name)}"

    # ── Check 6: icebreaker must not contain platitudes ─────
    if icebreaker:
        icebreaker_lower = icebreaker.lower()
        for platitude in GENERIC_PLATITUDES:
            if platitude in icebreaker_lower:
                logger.debug(
                    f"Rejected '{brand_name}': icebreaker contains "
                    f"generic platitude '{platitude}'"
                )
                return False, f"generic_icebreaker: {platitude}"

    # ── Check 7: brand must not be on enterprise blocklist ──
    brand_lower = brand_name.lower()
    for blocked in ENTERPRISE_BLOCKLIST:
        if blocked in brand_lower:
            logger.debug(f"Rejected enterprise brand: '{brand_name}'")
            return False, f"enterprise_blocklist: {blocked}"

    # ── Check 8: brand_name should not be purely numeric ────
    if brand_name.replace(" ", "").isdigit():
        return False, f"numeric_brand_name: {brand_name}"

    return True, "ok"
