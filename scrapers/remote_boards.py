"""
Closr — Remote Job Board RSS Scraper
Monitors WeWorkRemotely and RemoteOK for creator/influencer marketing
job postings, which signal active creator marketing budgets.
"""

import logging
from typing import Optional

from bs4 import BeautifulSoup
from dateutil import parser as dateutil_parser

from config import SCRAPER_TIMEOUT, DEEP_SCRAPE_ENABLED
from scrapers.base import BaseScraper, RawLead
from scrapers.polite_scraper import scrape_article

logger = logging.getLogger("closr.scrapers.remote_boards")

RSS_FEEDS = [
    {
        "name": "WeWorkRemotely",
        "url": "https://weworkremotely.com/categories/remote-marketing-jobs.rss",
    },
    {
        "name": "RemoteOK",
        "url": "https://remoteok.com/remote-jobs.rss",
    },
    # Note: Add your LinkedIn/Greenhouse feeds here if you have them!
]

# FIX: Actually included the broader terms mentioned in the comments, 
# plus root words to catch variations.
CREATOR_KEYWORDS = [
    # The Direct Budget Handlers (High Intent)
    "influencer", 
    "creator",
    "kol", 
    "talent manager",
    "partnerships",
    "partner marketing",
    "affiliate",
    
    # Platform specific signals
    "tiktok",
    "youtube",
    "instagram",
    
    # Broader net (Often handle creator budgets in lean startups)
    "social media",
    "community manager",
    "ugc"
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
                continue

        logger.info(f"Remote boards: {len(leads)} creator-budget job leads found")
        return leads

    def _parse_feed(self, feed_name: str, url: str) -> list[RawLead]:
        """Parse a single RSS feed and filter for creator marketing roles."""
        response = self.session.get(url, timeout=SCRAPER_TIMEOUT)
        response.raise_for_status()

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

            combined_text = (title + " " + (desc_tag.get_text() if desc_tag else "")).lower()
            
            # Filter check: Are any of our keywords in the title/summary?
            if not any(kw in combined_text for kw in CREATOR_KEYWORDS):
                continue

            description = ""
            if desc_tag:
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
            
            raw_text = f"Job Title: {title}\nDescription: {description}"
            
            # ── Deep Scrape Injection ──
            if DEEP_SCRAPE_ENABLED and link:
                try:
                    chunks = scrape_article(link)
                    if chunks:
                        logger.debug(f"Deep Scrape Success: {link}")
                        full_text = "\n\n".join(chunks)
                        raw_text = f"Job Title: {title}\nFull Posting:\n{full_text}"
                except Exception as e:
                    logger.debug(f"Deep scrape failed for {link}, falling back to summary: {e}")

            results.append(
                RawLead(
                    source=f"{self.source_name}_{feed_name.lower()}",
                    raw_text=raw_text,
                    url=link,
                    published_date=published,
                )
            )

        return results