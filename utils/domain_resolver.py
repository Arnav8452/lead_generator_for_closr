"""
Closr — Domain Resolution (Serper Integration)

Priority order:
1. Extract domain directly from scraped URL (highest confidence)
2. Fallback to Serper Google Search via discover_domain

Bad data costs enrichment API credits and produces garbage emails.
"""

import logging
from urllib.parse import urlparse
from enrichment.contact_discovery import discover_domain

logger = logging.getLogger("closr.utils.domain_resolver")

# In-memory cache — avoids redundant API calls within a pipeline run
_cache: dict[str, str | None] = {}

def resolve_domain(
    brand_name: str,
    niche: str | None = None,
    source_url: str | None = None,
) -> str | None:
    """
    Resolve a brand name to its primary domain.

    Priority:
    1. Extract domain from the source URL if it's the company's own site
    2. Fallback to Serper.dev

    Args:
        brand_name: Company name to look up
        niche: Optional industry vertical for disambiguation
        source_url: The URL where we found this company (may BE their domain)

    Returns:
        Domain string (e.g., "glossier.com") or None if not found.
    """
    if not brand_name or str(brand_name).lower() in ("null", "none", ""):
        return None

    cache_key = brand_name.strip().lower()
    if cache_key in _cache:
        return _cache[cache_key]

    # ── Strategy 1: Extract domain from source URL ──────────
    if source_url:
        url_domain = _extract_domain_from_url(source_url, brand_name)
        if url_domain:
            logger.info(f"Domain (URL extract): {brand_name} → {url_domain}")
            _cache[cache_key] = url_domain
            return url_domain

    # ── Strategy 2: Serper Domain Discovery ──────────
    domain = discover_domain(brand_name, niche=niche)
    _cache[cache_key] = domain
    return domain

def _extract_domain_from_url(url: str, brand_name: str) -> str | None:
    """
    If the source URL contains the brand name in the domain,
    extract it directly. Skips news sites, job boards, etc.
    """
    try:
        parsed = urlparse(url)
        domain = parsed.netloc.lower()
        if not domain:
            return None

        # Skip known aggregator/news domains — these are NOT the company
        skip_domains = {
            "news.google.com", "reddit.com", "news.ycombinator.com",
            "weworkremotely.com", "remoteok.com", "linkedin.com",
            "indeed.com", "glassdoor.com", "google.com",
            "upwork.com", "rss.app", "feedburner.com",
            "thesaasnews.com", "siliconangle.com", "techcrunch.com",
            "adweek.com", "prnewswire.com", "businesswire.com",
            "finsmes.com", "fundraiseinsider.com", "contentgrip.com",
            "pitchbook.com", "crunchbase.com", "vcnewsdaily.com",
        }
        base_domain = domain.replace("www.", "")
        if any(skip in base_domain for skip in skip_domains):
            return None

        # Check if brand name appears in the domain
        brand_clean = brand_name.lower().replace(" ", "").replace("-", "")
        domain_clean = base_domain.split(".")[0].replace("-", "")

        if brand_clean in domain_clean or domain_clean in brand_clean:
            return base_domain

    except Exception:
        pass
    return None
