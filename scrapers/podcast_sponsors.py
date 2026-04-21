"""
Closr — Podcast Sponsor RSS Scraper
Mines podcast show notes (RSS <description>) for active sponsor links.
Sponsor/promo/discount URLs in show notes = confirmed marketing budget.
"""

import logging
import re
from typing import Optional

from bs4 import BeautifulSoup
from dateutil import parser as dateutil_parser

from config import SCRAPER_TIMEOUT
from scrapers.base import BaseScraper, RawLead

logger = logging.getLogger("closr.scrapers.podcast_sponsors")

# FIX: More diverse podcast feeds including DTC/marketing-focused shows
# where sponsor reads are directly relevant to Closr's ICP
PODCAST_FEEDS = [
    # How I Built This (NPR) — extensive sponsor integrations
    "https://feeds.simplecast.com/qm_9xx0g",
    # Huberman Lab — premium DTC sponsor placements
    "https://feeds.megaphone.fm/hubermanlab",
    # My First Million — startup/DTC founders, high sponsor relevance
    "https://feeds.megaphone.fm/HS2300184645", 
    # Marketing Against the Grain — marketing-native audience
    "https://feeds.megaphone.fm/marketingagainstthegrain", 
]

# FIX: Original regex only matched href= attributes in HTML links.
# Podcast show notes are plain text or CDATA-wrapped, not HTML anchor tags.
# Use a general URL regex instead to catch all embedded sponsor URLs.
URL_REGEX = re.compile(
    r'https?://(?:[a-zA-Z0-9\-._~:/?#\[\]@!$&\'()*+,;=%]+)',
    re.IGNORECASE
)

# FIX: Expanded sponsor signals. "sponsor" and "promo" appear in URLs but
# "discount" rarely does. Added common sponsor URL patterns.
SPONSOR_KEYWORDS = [
    "sponsor",
    "promo",
    "/code/",
    "discount",
    "offer",
    "deal",
    "coupon",
    "try.",       # e.g. try.athleticgreens.com
    "get.",       # e.g. get.helix.com
    "shop.",      # e.g. shop.ysebeauty.com
]

# Cap episodes to process per feed to avoid massive RSS pulls
MAX_EPISODES_PER_FEED = 20


class PodcastSponsorScraper(BaseScraper):
    source_name = "podcast_sponsors"

    def fetch(self) -> list[RawLead]:
        """
        Parse podcast RSS feeds, extract URLs from show notes, and filter
        for sponsor/promo/discount links indicating active budgets.
        """
        leads: list[RawLead] = []

        for feed_url in PODCAST_FEEDS:
            try:
                leads.extend(self._parse_podcast_feed(feed_url))
            except Exception as e:
                logger.warning(f"Failed to parse podcast feed {feed_url}: {e}")
                continue

        logger.info(f"Podcast sponsors: {len(leads)} sponsor leads found")
        return leads

    def _parse_podcast_feed(self, feed_url: str) -> list[RawLead]:
        """Parse a single podcast RSS feed for sponsor links."""
        response = self.session.get(feed_url, timeout=SCRAPER_TIMEOUT)
        response.raise_for_status()

        soup = BeautifulSoup(response.content, "xml")
        items = soup.find_all("item")
        results: list[RawLead] = []

        for item in items[:MAX_EPISODES_PER_FEED]:
            title_tag = item.find("title")
            desc_tag = item.find("description")
            link_tag = item.find("link")
            pub_date_tag = item.find("pubDate")

            if not desc_tag:
                continue

            title = title_tag.get_text(strip=True) if title_tag else "Unknown Episode"

            # FIX: BeautifulSoup's get_text() on a CDATA-wrapped <description>
            # returns the raw HTML string, not rendered text. We need to unescape
            # HTML entities and then extract plain text before running URL regex.
            raw_description = str(desc_tag)

            # Strip the outer <description> tag wrapper
            import html
            description_text = html.unescape(raw_description)

            # Extract all URLs using the general URL regex (not href= only)
            all_urls = URL_REGEX.findall(description_text)

            # Filter for sponsor/promo/discount URLs
            sponsor_urls = [
                url for url in all_urls
                if any(kw in url.lower() for kw in SPONSOR_KEYWORDS)
            ]

            if not sponsor_urls:
                continue

            published: Optional[str] = None
            if pub_date_tag:
                try:
                    published = dateutil_parser.parse(
                        pub_date_tag.get_text(strip=True)
                    ).isoformat()
                except (ValueError, TypeError):
                    pass

            # Deduplicate sponsor URLs for clean output
            unique_sponsors = list(dict.fromkeys(sponsor_urls))[:10]

            raw_text = (
                f"Podcast: {feed_url}\n"
                f"Episode: {title}\n"
                f"Found active sponsor links:\n" +
                "\n".join(f"  - {url}" for url in unique_sponsors)
            )

            leads.append(
                RawLead(
                    source=self.source_name,
                    raw_text=raw_text,
                    url=link_tag.get_text(strip=True) if link_tag else feed_url,
                    published_date=published,
                )
            )

        return results
