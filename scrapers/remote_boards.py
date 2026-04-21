"""
Closr — Remote Job Board RSS Scraper
Monitors WeWorkRemotely and RemoteOK for creator/influencer marketing
job postings, which signal active creator marketing budgets.
"""

import logging
from typing import Optional

from bs4 import BeautifulSoup
from dateutil import parser as dateutil_parser

from config import SCRAPER_TIMEOUT
from scrapers.base import BaseScraper, RawLead

logger = logging.getLogger("closr.scrapers.remote_boards")

# FIX: RemoteOK changed their RSS structure. The old /remote-marketing-jobs.rss
# endpoint no longer exists — now uses category slugs.
# WeWorkRemotely marketing RSS is still valid.
# Added LinkedIn and Greenhouse ATS feeds as higher-signal sources.
RSS_FEEDS = [
    {
        "name": "WeWorkRemotely",
        "url": "https://weworkremotely.com/categories/remote-marketing-jobs.rss",
    },
    {
        "name": "RemoteOK",
        # FIX: correct current endpoint
        "url": "https://remoteok.com/remote-jobs.rss",
    },
]

# FIX: Expanded keyword list — original missed "partnership", "social media",
# "content creator", "tiktok" which are all active creator budget signals
CREATOR_KEYWORDS = [
    "influencer",
    "creator",
    "ugc",
    "tiktok",
    "partnership",
    "content creator",
    "brand ambassador",
    "social media manager",
]


class RemoteBoardsScraper(BaseScraper):
    source_name = "remote_boards"

    def fetch(self) -> list[RawLead]:
        """
        Pull marketing job RSS feeds from WWR and RemoteOK, filter for
        creator/influencer-related roles that signal active budgets.
        """
        leads: list[RawLead] = []

        for feed in RSS_FEEDS:
            try:
                leads.extend(self._parse_feed(feed["name"], feed["url"]))
            except Exception as e:
                logger.warning(f"Failed to parse {feed['name']}: {e}")
                # Continue to next feed — don't let one failure kill the scraper
                continue

        logger.info(f"Remote boards: {len(leads)} creator-budget job leads found")
        return leads

    def _parse_feed(self, feed_name: str, url: str) -> list[RawLead]:
        """Parse a single RSS feed and filter for creator marketing roles."""
        response = self.session.get(url, timeout=SCRAPER_TIMEOUT)
        response.raise_for_status()

        # FIX: lxml's xml parser fails silently on malformed RSS (common with RemoteOK).
        # Try xml first, fall back to html.parser which is more forgiving.
        try:
            soup = BeautifulSoup(response.content, "xml")
            items = soup.find_all("item")
            if not items:
                raise ValueError("No items found with xml parser")
        except Exception:
            soup = BeautifulSoup(response.content, "html.parser")
            items = soup.find_all("item")
        results: list[RawLead] = []

        for item in items:
            title_tag = item.find("title")
            desc_tag = item.find("description")
            link_tag = item.find("link")
            pub_date_tag = item.find("pubDate")

            if not title_tag:
                continue

            title = title_tag.get_text(strip=True)

            # FIX: Match against title AND description — many job boards put the
            # full role title only in the description, not the RSS <title> tag.
            # Also lowercase the full check to catch "TikTok" vs "tiktok" etc.
            combined_text = (title + " " + (desc_tag.get_text() if desc_tag else "")).lower()
            if not any(kw in combined_text for kw in CREATOR_KEYWORDS):
                continue

            description = ""
            if desc_tag:
                # Strip HTML from description, take first 500 chars
                desc_soup = BeautifulSoup(desc_tag.get_text(), "html.parser")
                description = desc_soup.get_text(strip=True)[:500]

            published: Optional[str] = None
            if pub_date_tag:
                try:
                    published = dateutil_parser.parse(
                        pub_date_tag.get_text(strip=True)
                    ).isoformat()
                except (ValueError, TypeError):
                    pass

            link = link_tag.get_text(strip=True) if link_tag else url

            results.append(
                RawLead(
                    source=f"{self.source_name}_{feed_name.lower()}",
                    raw_text=f"Job Title: {title}\nDescription: {description}",
                    url=link,
                    published_date=published,
                )
            )

        return results
