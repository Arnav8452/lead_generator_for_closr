"""
Closr — Clearbit Domain Resolution (Free, Unauthenticated)
Uses the Clearbit autocomplete API to resolve a brand name into a domain.
No API key required. Rate-limited by IP — use sparingly.
"""

import logging
from difflib import SequenceMatcher

import requests

from config import SCRAPER_TIMEOUT

logger = logging.getLogger("closr.enrichment.clearbit")

# Clearbit's free, unauthenticated autocomplete endpoint
CLEARBIT_URL = "https://autocomplete.clearbit.com/v1/companies/suggest"

# Minimum fuzzy match ratio between the query brand name and the
# returned company name. Prevents wildly incorrect domain resolution.
MIN_MATCH_RATIO = 0.55


def resolve_domain(brand_name: str, _cache: dict = {}) -> str | None:
    """
    Resolve a brand name to its primary domain using Clearbit autocomplete.

    Uses an in-memory cache (mutable default arg) to avoid redundant API calls
    within the same pipeline run.

    Args:
        brand_name: The company/brand name to look up.
        _cache: Internal call-level cache. Do not pass explicitly.

    Returns:
        The resolved domain string (e.g. "glossier.com") or None if not found.
    """
    # Normalize for cache lookup
    cache_key = brand_name.strip().lower()

    if cache_key in _cache:
        logger.debug(f"Clearbit cache hit for '{brand_name}'")
        return _cache[cache_key]

    try:
        response = requests.get(
            CLEARBIT_URL,
            params={"query": brand_name},
            timeout=SCRAPER_TIMEOUT,
        )
        response.raise_for_status()
        suggestions = response.json()

        if not suggestions or not isinstance(suggestions, list):
            logger.info(f"Clearbit: No suggestions for '{brand_name}'")
            _cache[cache_key] = None
            return None

        # Find the best match by fuzzy-comparing the brand name
        best_match: dict | None = None
        best_ratio: float = 0.0

        for suggestion in suggestions:
            company_name = suggestion.get("name", "")
            ratio = SequenceMatcher(
                None,
                cache_key,
                company_name.lower(),
            ).ratio()

            if ratio > best_ratio:
                best_ratio = ratio
                best_match = suggestion

        if best_match and best_ratio >= MIN_MATCH_RATIO:
            domain = best_match.get("domain", "")
            if domain:
                logger.info(
                    f"Clearbit: '{brand_name}' → {domain} "
                    f"(match: {best_ratio:.2f})"
                )
                _cache[cache_key] = domain
                return domain

        logger.info(
            f"Clearbit: No confident match for '{brand_name}' "
            f"(best ratio: {best_ratio:.2f} < {MIN_MATCH_RATIO})"
        )
        _cache[cache_key] = None
        return None

    except requests.exceptions.Timeout:
        logger.warning(f"Clearbit: Timeout resolving '{brand_name}'")
        return None
    except requests.exceptions.RequestException as e:
        logger.warning(f"Clearbit: Request failed for '{brand_name}': {e}")
        return None
    except (ValueError, KeyError) as e:
        logger.warning(f"Clearbit: Parse error for '{brand_name}': {e}")
        return None
