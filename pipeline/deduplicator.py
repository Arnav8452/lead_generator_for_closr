"""
Closr — Lead Deduplicator
Prevents duplicate brand processing within a single pipeline run
and across the daily pool in Supabase.

Normalization: strips common suffixes (Inc, LLC, Labs, Co, Ltd, Corp)
and whitespace to match "Glossier Inc" = "glossier" = "Glossier Labs".
"""

import logging
import re

from db.supabase_client import check_duplicate

logger = logging.getLogger("closr.pipeline.deduplicator")

# Common company suffixes to strip during normalization.
COMPANY_SUFFIXES = re.compile(
    r'\b(inc\.?|llc\.?|ltd\.?|co\.?|corp\.?|labs?\.?|studio|'
    r'limited|incorporated|corporation|group|holdings?|'
    r'technologies|solutions|enterprises?)\b\.?',
    re.IGNORECASE,
)


class Deduplicator:
    """
    Tracks processed brand names within a pipeline run (in-memory set)
    and checks against the existing daily pool (Supabase query).
    """

    def __init__(self):
        self._seen: set[str] = set()

    def is_duplicate(self, brand_name: str) -> bool:
        """
        Check if this brand has already been processed in this run
        or exists in the current daily pool.

        Args:
            brand_name: The raw brand name from LLM extraction.

        Returns:
            True if duplicate (should be skipped), False if new.
        """
        normalized = self.normalize(brand_name)

        if not normalized:
            logger.debug(f"Dedup: Empty brand name after normalization")
            return True

        # Check 1: Already processed in this pipeline run
        if normalized in self._seen:
            logger.debug(f"Dedup: '{brand_name}' already seen in this run")
            return True

        # Check 2: Already exists in today's daily pool
        # Use normalized name to match consistently with in-memory check
        if check_duplicate(normalized):
            logger.debug(f"Dedup: '{brand_name}' already in daily pool")
            self._seen.add(normalized)
            return True

        # Mark as seen for this run
        self._seen.add(normalized)
        return False

    @staticmethod
    def normalize(brand_name: str) -> str:
        """
        Normalize a brand name for deduplication comparison.

        Steps:
        1. Lowercase
        2. Strip company suffixes (Inc, LLC, Labs, etc.)
        3. Remove orphaned punctuation
        4. Collapse extra whitespace
        """
        if not brand_name:
            return ""

        normalized = brand_name.lower().strip()
        # Remove company suffixes (including trailing dots)
        normalized = COMPANY_SUFFIXES.sub("", normalized)
        # Remove orphaned dots and commas left after suffix stripping
        normalized = re.sub(r'[.,]+', '', normalized)
        # Collapse multiple spaces
        normalized = re.sub(r'\s+', ' ', normalized).strip()

        return normalized

    @property
    def seen_count(self) -> int:
        """Number of unique brands processed in this run."""
        return len(self._seen)

    def reset(self) -> None:
        """Clear the in-memory dedup set for a new run."""
        self._seen.clear()
