"""
Closr — Google News Funding Scraper (Phase 1: Deep Scraping)
Scrapes Google News RSS for funding events and marketing distress signals,
then deep-scrapes articles concurrently for full-text extraction.

Concurrency model:
  - RSS queries run sequentially (Google rate limits)
  - Lexical pre-filter blocks directories and macro-trend reports
  - Air-Lock filters headlines before any network fetch
  - Deep scrapes (unroll + Jina/Readability) run concurrently
    via ThreadPoolExecutor(max_workers=5) to respect Jina's
    free-tier concurrency limit of 5. Readability is local so
    it never hits the limit.
"""

import urllib.parse
import xml.etree.ElementTree as ET
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from curl_cffi import requests
from scrapers.base import BaseScraper, RawLead
from scrapers.polite_scraper import scrape_article, unroll_google_link
from pipeline.extractor import extractor

logger = logging.getLogger("closr.scrapers.google_news_funding")

# Jina free tier: Concurrency 5. Never exceed this.
_DEEP_SCRAPE_WORKERS = 5

def _deep_scrape_article(item_data: dict) -> dict | None:
    """
    Worker function: unroll Google redirect + scrape article.
    Returns enriched item_data dict or None on total failure.
    Runs in a thread pool — must be stateless and thread-safe.
    """
    link = item_data["link"]
    title = item_data["title"]
    description = item_data["description"]
    pub_date = item_data["pub_date"]

    try:
        real_url = unroll_google_link(link)
        article_chunks = scrape_article(real_url)

        if article_chunks:
            full_body = " ".join(article_chunks)
            full_text = (
                f"Headline: {title}\n"
                f"Published: {pub_date or 'Unknown'}\n"
                f"Full Article:\n{full_body}"
            )
            logger.info(f"  Deep-scraped: {title[:60]}... ({len(full_body)} chars)")
            return {
                "full_text": full_text,
                "url": real_url,
                "title": title,
                "pub_date": pub_date,
                "scraped": True,
            }
        else:
            # Jina failed, Readability failed — fall back to RSS snippet
            full_text = f"Headline: {title}\nSummary: {description}"
            logger.debug(f"  RSS-only (deep scrape failed): {title[:60]}...")
            return {
                "full_text": full_text,
                "url": link,
                "title": title,
                "pub_date": pub_date,
                "scraped": False,
            }

    except Exception as e:
        logger.warning(f"  Deep scrape error for '{title[:60]}': {e}")
        # Still return RSS snippet rather than losing the lead entirely
        full_text = f"Headline: {title}\nSummary: {description}"
        return {
            "full_text": full_text,
            "url": link,
            "title": title,
            "pub_date": pub_date,
            "scraped": False,
        }


class GoogleNewsFundingScraper(BaseScraper):
    source_name = "Google News (Funding Signals)"

    def fetch(self) -> list[RawLead]:
        leads = []
        seen_urls = set()

        # 1. Base Parameters (Funding Events + Marketing Distress Signals)
        triggers = (
            '(intitle:"Series A" OR intitle:"Series B" OR intitle:"Seed" OR '
            'intitle:"Pre-seed" OR intitle:"stealth" OR intitle:"funding" OR '
            'intitle:"raises" OR intitle:"raised" OR intitle:"secures" OR '
            '"customer acquisition cost" OR "ROAS" OR "ad spend" OR '
            '"influencer marketing" OR "UGC" OR "creator sponsorships")'
        )

        # THE NICHES: The Brands Most Likely to Sponsor Creators
        niches = (
            '("SaaS" OR "B2B SaaS" OR "DTC" OR "D2C" OR "CPG" OR '
            '"e-commerce" OR "creator economy" OR "skincare" OR "beauty" OR "wellness")'
        )

        # 2. The Broad Net (Open Internet)
        broad_query = f'{triggers}'
        queries = [broad_query]

        # 3. The Verified Platforms (Batched into groups of 3)
        verified_sites = [
            "finsmes.com", "siliconangle.com", "techfundingnews.com", "businesswire.com",
            "news.crunchbase.com", "prnewswire.com", "vcnewsdaily.com", "pitchbook.com",
            "fintechfutures.com", "vestbee.com", "growthlist.co", "fundup.ai",
            "fundraiseinsider.com", "fundtq.com", "scouts.yutori.com"
        ]

        for i in range(0, len(verified_sites), 3):
            batch = verified_sites[i:i+3]
            sites_str = " ".join([f"site:{site}" for site in batch])
            queries.append(f'({sites_str}) AND {triggers} AND {niches}')

        # ── Phase A: Collect all Air-Lock survivors across all queries ──
        pending: list[dict] = []   # items that passed Air-Lock, awaiting deep scrape

        for query in queries:
            safe_query = urllib.parse.quote(query)
            url = f"https://news.google.com/rss/search?q={safe_query}+when:1d&hl=en-US&gl=US&ceid=US:en"

            try:
                res = requests.get(url, impersonate="safari17_0", timeout=15)
                if res.status_code != 200:
                    logger.warning(f"[{self.source_name}] Blocked with status: {res.status_code}")
                    continue

                root = ET.fromstring(res.text)

                for item in root.findall('.//item')[:15]:
                    link = item.find('link').text if item.find('link') is not None else ""

                    if not link or link in seen_urls:
                        continue

                    title = item.find('title').text if item.find('title') is not None else ""
                    
                    # ── V2.5: BALANCED LEXICAL PRE-FILTER ──
                    # 1. Structural URL Blocklist (Targets directories & profiles, leaves articles alone)
                    bad_url_paths = [
                        "/profile/", "/company/", "/author/", "/category/", 
                        "/tag/", "/directory/", "/organization/", "/person/"
                    ]
                    if any(path in link.lower() for path in bad_url_paths):
                        logger.debug(f"  [{self.source_name}] Blocklist URL DROP: {link}")
                        continue

                    # 2. Structural Title Blocklist (Targets macro-reports and roundups, leaves news events alone)
                    bad_title_phrases = [
                        "company profile", "funding trends", "market report", 
                        "weekly recap", "funding roundup", "market overview", 
                        "state of venture", "industry report"
                    ]
                    title_lower = title.lower()
                    if any(phrase in title_lower for phrase in bad_title_phrases):
                        logger.debug(f"  [{self.source_name}] Blocklist TITLE DROP: {title}")
                        continue

                    description = item.find('description').text if item.find('description') is not None else ""
                    pub_date = item.find('pubDate').text if item.find('pubDate') is not None else None

                    # ── Air-Lock Early Exit ──
                    if not extractor.airlock(title):
                        logger.debug(f"  [{self.source_name}] Air-Lock DROP (skipped fetch): {title[:60]}")
                        continue

                    seen_urls.add(link)
                    pending.append({
                        "link": link,
                        "title": title,
                        "description": description,
                        "pub_date": pub_date,
                    })

            except Exception as e:
                logger.error(f"[{self.source_name}] Error parsing XML: {e}")

        if not pending:
            logger.info(f"[{self.source_name}] No articles passed filters.")
            return []

        logger.info(
            f"[{self.source_name}] {len(pending)} articles passed Air-Lock. "
            f"Deep-scraping concurrently (workers={_DEEP_SCRAPE_WORKERS})..."
        )

        # ── Phase B: Concurrent deep scraping ──
        with ThreadPoolExecutor(max_workers=_DEEP_SCRAPE_WORKERS) as executor:
            future_to_item = {
                executor.submit(_deep_scrape_article, item): item
                for item in pending
            }
            for future, item in future_to_item.items():
                result = future.result()
                if result:
                    leads.append(RawLead(
                        source=self.source_name,
                        raw_text=result["full_text"],
                        url=result["url"],
                        published_date=result["pub_date"],
                        brand_name_hint=result["title"],
                    ))

        logger.info(f"[{self.source_name}] Total leads: {len(leads)} (deep-scraped)")
        return leads