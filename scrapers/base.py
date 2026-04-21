"""
Closr — Base Scraper Interface
All scrapers inherit from BaseScraper and implement the fetch() method.
The run() wrapper provides standardized error handling, logging, and retry logic.
"""

import logging
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional

import requests

from config import SCRAPER_MAX_RETRIES

logger = logging.getLogger("closr.scrapers")


@dataclass
class RawLead:
    """
    Uniform container for raw scraper output before LLM extraction.
    Every scraper produces a list of these. The pipeline then feeds
    raw_text into the LLM for structured brand extraction.
    """
    source: str
    raw_text: str
    url: str
    published_date: Optional[str] = None
    brand_name_hint: Optional[str] = None


class BaseScraper(ABC):
    """
    Abstract base class for all Closr data scrapers.

    Subclasses must set `source_name` and implement `fetch()`.
    Call `run()` to execute with automatic retry and error handling.
    """
    source_name: str = "unknown"

    def __init__(self):
        # Shared HTTP session for TCP/TLS connection reuse across requests.
        # Subclasses using the `requests` library should use self.session
        # instead of bare requests.get()/requests.post().
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "Closr/1.0 (lead-engine)",
        })

    @abstractmethod
    def fetch(self) -> list[RawLead]:
        """
        Perform the actual scraping logic. Must return a list of RawLead
        objects. Raise exceptions on failure — the run() wrapper catches them.
        """
        ...

    def run(self) -> list[RawLead]:
        """
        Execute fetch() with retry logic and structured error handling.
        Returns an empty list on total failure rather than crashing the pipeline.
        """
        last_error: Exception | None = None

        for attempt in range(1, SCRAPER_MAX_RETRIES + 1):
            try:
                leads = self.fetch()
                logger.info(
                    f"[{self.source_name}] Scraped {len(leads)} raw leads "
                    f"(attempt {attempt}/{SCRAPER_MAX_RETRIES})"
                )
                return leads
            except Exception as e:
                last_error = e
                logger.warning(
                    f"[{self.source_name}] Attempt {attempt}/{SCRAPER_MAX_RETRIES} "
                    f"failed: {e}"
                )
                if attempt < SCRAPER_MAX_RETRIES:
                    # Exponential backoff: 2s, 4s, 8s …
                    backoff = 2 ** attempt
                    logger.info(f"[{self.source_name}] Retrying in {backoff}s…")
                    time.sleep(backoff)

        logger.error(
            f"[{self.source_name}] All {SCRAPER_MAX_RETRIES} attempts failed. "
            f"Last error: {last_error}"
        )
        return []
