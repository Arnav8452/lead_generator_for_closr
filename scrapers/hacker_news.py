import time
import urllib.parse
import logging
from curl_cffi import requests
from scrapers.base import BaseScraper, RawLead

logger = logging.getLogger("closr.scrapers")

class HackerNewsDistressScraper(BaseScraper):
    source_name = "Hacker News (Distressed Sentiment)"

    def fetch(self) -> list[RawLead]:
        leads = []
        url = "https://hn.algolia.com/api/v1/search_by_date"

        # FIX: Exact string queries like '"CAC is too high"' match near-zero HN comments
        # because nobody on HN phrases it that way. HN founders say "our CAC",
        # "conversion rate", "paid acquisition", "influencer ROI" etc.
        # Switched to short keyword queries that actually appear in HN discourse.
        # Also added story tags — "Ask HN: ..." posts are gold for intent signals.
        queries = [
            {"q": "customer acquisition cost", "tags": "comment,story"},
            {"q": "ROAS", "tags": "comment"},
            {"q": "influencer marketing", "tags": "comment,story"},
            {"q": "ad spend", "tags": "comment"},
        ]

        # Expand to a 14-day lookback window
        lookback_ts = int(time.time()) - (86400 * 14)

        for q_config in queries:
            query = q_config["q"]
            tags = q_config["tags"]

            params = {
                "query": query,
                "tags": tags,
                "numericFilters": f"created_at_i>{lookback_ts}",
                "hitsPerPage": 10,
            }
            # FIX: HN Algolia is a public JSON API — no TLS fingerprinting needed.
            # Using impersonate="chrome120" wastes resources and can cause issues.
            # Switched to plain requests.get with no impersonation.
            try:
                res = requests.get(url, params=params, timeout=10)
                if res.status_code != 200:
                    logger.warning(f"[{self.source_name}] HN API error {res.status_code} for '{query}'")
                    continue

                data = res.json()
                hits = data.get("hits", [])
                logger.info(f"[{self.source_name}] '{query}' → {len(hits)} hits")

                for hit in hits:
                    author = hit.get("author", "Unknown")
                    # FIX: HN returns comment_text for comments, title for stories
                    # Original code only checked comment_text, missing all story hits
                    comment_text = hit.get("comment_text") or hit.get("title") or ""
                    story_title = hit.get("story_title") or hit.get("title") or ""
                    thread_id = hit.get("story_id") or hit.get("objectID") or ""

                    if not comment_text:
                        continue

                    full_text = (
                        f"Author: {author}\n"
                        f"Platform: Hacker News\n"
                        f"Thread: {story_title}\n"
                        f"Comment: {comment_text}"
                    )
                    link = f"https://news.ycombinator.com/item?id={thread_id}"

                    leads.append(RawLead(
                        source=self.source_name,
                        raw_text=full_text,
                        url=link,
                    ))

            except Exception as e:
                logger.error(f"[{self.source_name}] Error for '{query}': {e}")

        logger.info(f"Hacker News: {len(leads)} leads found")
        return leads
