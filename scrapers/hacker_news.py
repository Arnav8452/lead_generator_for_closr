import time
import logging
import requests
from bs4 import BeautifulSoup
from concurrent.futures import ThreadPoolExecutor
from config import DEEP_SCRAPE_ENABLED
from scrapers.base import BaseScraper, RawLead
from scrapers.polite_scraper import scrape_article

logger = logging.getLogger("closr.scrapers.hn_distress")

# Limit concurrency to avoid getting IP-banned by Jina/Algolia
_DEEP_SCRAPE_WORKERS = 5

def _process_hn_hit(hit_data: dict) -> RawLead | None:
    """
    Stateless worker to concurrently deep-scrape linked HN articles.
    """
    full_text = hit_data["base_text"]
    story_url = hit_data["story_url"]
    thread_id = hit_data["thread_id"]
    source_name = hit_data["source_name"]

    # ── Deep Scrape Injection ──
    if DEEP_SCRAPE_ENABLED and story_url:
        try:
            chunks = scrape_article(story_url)
            if chunks:
                logger.debug(f"Deep Scrape Success: {story_url}")
                full_article = "\n\n".join(chunks)
                full_text += f"\n\nFull Article Context:\n{full_article}"
        except Exception as e:
            logger.debug(f"Deep scrape failed for HN story {story_url}, falling back: {e}")

    link = f"https://news.ycombinator.com/item?id={thread_id}"

    return RawLead(
        source=source_name,
        raw_text=full_text,
        url=link,
    )

class HackerNewsDistressScraper(BaseScraper):
    source_name = "Hacker News (Distressed Sentiment)"

    def fetch(self) -> list[RawLead]:
        leads = []
        seen_ids = set()
        pending_hits = [] 
        
        url = "https://hn.algolia.com/api/v1/search_by_date"

        queries = [
            {"q": "CAC", "tags": "comment"}, 
            {"q": "customer acquisition", "tags": "(comment,story)"},
            {"q": "ROAS", "tags": "comment"},
            {"q": "ad spend", "tags": "comment"},
            {"q": "Facebook ads expensive", "tags": "comment"},
            {"q": "Google ads expensive", "tags": "comment"},
            {"q": "ads are getting too expensive", "tags": "comment"},
            {"q": "burning money on ads", "tags": "comment"},
            {"q": "influencer marketing", "tags": "(comment,story)"},
            {"q": "creator economy", "tags": "(comment,story)"},
            {"q": "sponsoring YouTubers", "tags": "comment"},
            {"q": "newsletter sponsorships", "tags": "comment"},
            {"q": "sponsor our", "tags": "comment"}
        ]

        # 86400 seconds = 24 hours. Only fetch fresh daily complaints.
        lookback_ts = int(time.time()) - (86400 * 1)

        # Phase A: Collect hits sequentially from Algolia API
        for q_config in queries:
            query = q_config["q"]
            tags = q_config["tags"]

            params = {
                "query": query,
                "tags": tags,
                "numericFilters": f"created_at_i>{lookback_ts}",
                "hitsPerPage": 50,
            }
            
            try:
                res = requests.get(url, params=params, timeout=10)
                
                if res.status_code != 200:
                    logger.warning(f"[{self.source_name}] HN API error {res.status_code} for '{query}'")
                    continue

                data = res.json()
                hits = data.get("hits", [])
                logger.debug(f"[{self.source_name}] '{query}' → {len(hits)} hits")

                for hit in hits:
                    object_id = str(hit.get("objectID", ""))
                    
                    if not object_id or object_id in seen_ids:
                        continue
                    
                    seen_ids.add(object_id)

                    author = hit.get("author", "Unknown")
                    raw_comment = hit.get("comment_text") or hit.get("title") or ""
                    story_title = hit.get("story_title") or hit.get("title") or ""
                    thread_id = hit.get("story_id") or object_id

                    if not raw_comment:
                        continue

                    clean_comment = BeautifulSoup(raw_comment, "html.parser").get_text(separator=" ", strip=True)
                    story_url = hit.get("url") or hit.get("story_url")

                    base_text = (
                        f"Author: {author}\n"
                        f"Platform: Hacker News\n"
                        f"Thread: {story_title}\n"
                        f"Comment: {clean_comment}"
                    )

                    pending_hits.append({
                        "base_text": base_text,
                        "story_url": story_url,
                        "thread_id": thread_id,
                        "source_name": self.source_name
                    })

            except Exception as e:
                logger.error(f"[{self.source_name}] Error for '{query}': {e}")

        if not pending_hits:
            logger.info(f"[{self.source_name}] No hits found to process.")
            return []

        logger.info(f"[{self.source_name}] {len(pending_hits)} hits queued. Deep-scraping concurrently...")

        # Phase B: Concurrent deep scraping
        with ThreadPoolExecutor(max_workers=_DEEP_SCRAPE_WORKERS) as executor:
            future_to_hit = {
                executor.submit(_process_hn_hit, hit): hit 
                for hit in pending_hits
            }
            for future in future_to_hit:
                result = future.result()
                if result:
                    leads.append(result)

        logger.info(f"Hacker News: {len(leads)} distinct leads extracted")
        return leads