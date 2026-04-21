"""
Closr — Meta Ad Library Scraper (Official Graph API)
Uses the public Meta Ad Library API to detect brands with abnormally
high ad activity, which signals active marketing budgets.

Requires a valid META_ACCESS_TOKEN from developers.facebook.com.
"""

import logging
from collections import defaultdict

from config import (
    META_ACCESS_TOKEN,
    META_ADS_SPIKE_THRESHOLD,
    SCRAPER_TIMEOUT,
)
from scrapers.base import BaseScraper, RawLead

logger = logging.getLogger("closr.scrapers.meta_ads")

# Official Meta Graph API endpoint for the Ad Library
ADS_ARCHIVE_URL = "https://graph.facebook.com/v18.0/ads_archive"

# Search terms covering high-budget consumer verticals
SEARCH_TERMS = ["skincare", "fashion", "tech", "supplements"]

# Required fields from the Ad Library API
AD_FIELDS = "ad_snapshot_url,page_name,ad_delivery_start_time,ad_creative_bodies"


class MetaAdsScraper(BaseScraper):
    source_name = "meta_ads"

    def fetch(self) -> list[RawLead]:
        """
        Query the Meta Ad Library for each search term, aggregate results
        by page_name, and yield leads for brands exceeding the ad spike
        threshold (high ad volume = active marketing budget).
        """
        if not META_ACCESS_TOKEN:
            logger.warning(
                "META_ACCESS_TOKEN not set — skipping Meta Ads scraper."
            )
            return []

        # Aggregate ad counts per brand (page_name) across all search terms
        brand_ads: defaultdict[str, list[dict]] = defaultdict(list)

        for term in SEARCH_TERMS:
            try:
                ads = self._search_ads(term)
                for ad in ads:
                    page_name = ad.get("page_name", "").strip()
                    if page_name:
                        brand_ads[page_name].append(ad)
            except Exception as e:
                logger.warning(f"Meta Ads search for '{term}' failed: {e}")
                continue

        # Filter to brands with ad count exceeding the spike threshold
        leads: list[RawLead] = []
        for page_name, ads in brand_ads.items():
            if len(ads) >= META_ADS_SPIKE_THRESHOLD:
                # Build a summary of ad creative text for LLM context
                creative_samples = []
                for ad in ads[:5]:  # Sample up to 5 ad creatives
                    bodies = ad.get("ad_creative_bodies", [])
                    if bodies:
                        creative_samples.append(bodies[0][:200])

                raw_text = (
                    f"Brand: {page_name}\n"
                    f"Active Ads: {len(ads)}\n"
                    f"Sample Creatives:\n" +
                    "\n".join(f"  - {c}" for c in creative_samples)
                )

                leads.append(
                    RawLead(
                        source=self.source_name,
                        raw_text=raw_text,
                        url=f"https://www.facebook.com/ads/library/?active_status=all&ad_type=all&q={page_name}",
                        brand_name_hint=page_name,
                    )
                )

        logger.info(
            f"Meta Ads: {len(leads)} brands with >{META_ADS_SPIKE_THRESHOLD} "
            f"active ads detected"
        )
        return leads

    def _search_ads(self, search_term: str) -> list[dict]:
        """
        Execute a single Ad Library API query for a given search term.
        Handles pagination via the 'after' cursor.
        """
        all_ads: list[dict] = []
        params: dict = {
            "access_token": META_ACCESS_TOKEN,
            "search_terms": search_term,
            "ad_reached_countries": '["US"]',
            "ad_active_status": "ACTIVE",
            "fields": AD_FIELDS,
            "limit": 100,
        }

        url = ADS_ARCHIVE_URL
        max_pages = 5  # Cap pagination to avoid runaway requests

        for page in range(max_pages):
            response = self.session.get(url, params=params, timeout=SCRAPER_TIMEOUT)
            response.raise_for_status()
            data = response.json()

            ads = data.get("data", [])
            all_ads.extend(ads)

            # Check for next page
            paging = data.get("paging", {})
            next_url = paging.get("next")
            if not next_url or not ads:
                break

            # Use the next URL directly (it includes cursor params)
            url = next_url
            params = {}  # params are embedded in the next URL

        return all_ads
