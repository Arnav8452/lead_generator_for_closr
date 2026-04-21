"""
Closr — Reddit Stealth Scraper (No API Keys Required)
Uses curl_cffi to impersonate a Chrome browser's TLS fingerprint,
bypassing Reddit's 403 blocks on the unauthenticated .json endpoints.

Performs deep comment extraction for each thread — brand names and intent
signals are often revealed in the comment section, not just the OP.

Search targets both founder pain signals (CAC/ROAS/ad spend) and direct
creator hiring/collaboration opportunities.
"""

import logging
import time
import urllib.parse

from curl_cffi import requests

from config import SCRAPER_TIMEOUT
from scrapers.base import BaseScraper, RawLead

logger = logging.getLogger("closr.scrapers.reddit_stealth")

HEADERS = {"User-Agent": "python:closr_stealth_scraper:v1.0 (by /u/arnav_chandra)"}

# FIX: Split into focused per-intent queries.
# Reddit's search engine silently returns 0 results for long multi-clause boolean
# queries with mixed AND/OR logic — it doesn't support them reliably.
# Separate short queries per signal type work far better.
SEARCH_QUERIES = [
    # Pain signal: CAC/ROAS distress
    "CAC ROAS ad spend",
    # Direct hiring signal
    "hiring UGC creator",
    # Collaboration signal
    "looking for influencer collab",
]

# Target subreddits where founders express marketing pain or seek creators
TARGET_SUBREDDITS = "SaaS+marketing+DTC+Entrepreneur"

# Cap to prevent rate-limiting on residential IPs
SEARCH_LIMIT = 20

# FIX: Reduced from 50 — many genuine intent posts are short (e.g. "Anyone know
# a good UGC creator for skincare? Budget $2k"). 50 chars filtered almost everything.
MIN_SELFTEXT_LENGTH = 20

# Sleep between thread requests to avoid rate-limiting
THREAD_DELAY_SECONDS = 1.5

# Max top-level comments to extract per thread
MAX_COMMENTS_PER_THREAD = 5

# FIX: safari17_0 is not a valid curl_cffi impersonate target.
# Valid values as of curl_cffi 0.6+: chrome110, chrome119, chrome120, chrome124,
# safari15_3, safari15_5, safari_ios16_5. Use chrome120 — best general coverage.
BROWSER_IMPERSONATE = "chrome120"


class RedditStealthScraper(BaseScraper):
    source_name = "reddit_stealth"

    def fetch(self) -> list[RawLead]:
        """
        1. Run each focused query against target subreddits separately.
           (One short query per signal type — Reddit search is unreliable
           with long multi-clause boolean expressions and silently returns 0.)
        2. For each matching thread, dive in to extract top comments.
        3. Combine OP context + comment context for richer LLM extraction.
        """
        leads: list[RawLead] = []
        seen_urls: set[str] = set()  # dedup across queries

        for query in SEARCH_QUERIES:
            safe_query = urllib.parse.quote(query)
            # Using RSS endpoint to bypass stringent JSON WAF blocks
            search_url = (
                f"https://www.reddit.com/r/{TARGET_SUBREDDITS}/search.rss"
                f"?q={safe_query}&restrict_sr=on&sort=new&t=week&limit={SEARCH_LIMIT}"
            )

            try:
                # Add compliant scraping header
                res = requests.get(search_url, headers=HEADERS, timeout=15)
                
                if res.status_code == 403:
                    logger.warning(f"Reddit search blocked (status 403) for query '{query}'. Skipping.")
                    continue
                res.raise_for_status()

                # Parse Reddit RSS XML
                soup = BeautifulSoup(res.content, "xml")
                entries = soup.find_all("entry")

                for entry in entries:
                    title_elem = entry.find("title")
                    content_elem = entry.find("content")
                    link_elem = entry.find("link")
                    author_elem = entry.find("author")
                    
                    if not title_elem or not link_elem:
                        continue

                    title = title_elem.text
                    selftext = content_elem.text if content_elem else ""
                    url = link_elem.get("href")
                    author = author_elem.find("name").text if author_elem and author_elem.find("name") else ""
                    permalink = url.replace("https://www.reddit.com", "")

                    # Skip deleted/removed posts
                    if selftext in ("[deleted]", "[removed]", ""):
                        selftext = ""

                    # Skip posts with no meaningful content
                    if len(selftext) < MIN_SELFTEXT_LENGTH and len(title) < 30:
                        continue

                    # Dedup across queries
                    if permalink in seen_urls:
                        continue
                    seen_urls.add(permalink)

                    comments_text = self._extract_thread_comments(permalink)

                    full_context = (
                        f"Author: {author}\n"
                        f"Title: {title}\n"
                        f"Body: {selftext[:1000]}\n"
                        f"\nTop Comments:{comments_text}"
                    )

                    leads.append(
                        RawLead(
                            source=self.source_name,
                            raw_text=full_context,
                            url=f"https://www.reddit.com{permalink}",
                        )
                    )

                # Polite delay between queries
                time.sleep(1.0)

            except Exception as e:
                logger.error(f"Reddit stealth scraper error for query '{query}': {e}")
                continue  # FIX: was raise — one bad query killed all subsequent queries

        logger.info(f"Reddit stealth: {len(leads)} leads after filtering")
        return leads

    def _extract_thread_comments(self, permalink: str) -> str:
        """
        Fetch the full thread JSON and extract the top N comments.
        Reddit thread JSON structure: [post_data, comment_tree]

        Returns a formatted string of comment text for LLM context.
        """
        thread_url = f"https://www.reddit.com{permalink}.json"

        # Critical: sleep to prevent rate-limiting on residential IP
        time.sleep(THREAD_DELAY_SECONDS)

        try:
            thread_res = requests.get(
                thread_url,
                headers=HEADERS,
                timeout=SCRAPER_TIMEOUT,
            )

            if thread_res.status_code != 200:
                logger.debug(
                    f"Thread fetch failed ({thread_res.status_code}): {permalink}"
                )
                return ""

            thread_data = thread_res.json()

            # Reddit thread JSON is a list: [0] is the post, [1] is comments
            if not isinstance(thread_data, list) or len(thread_data) < 2:
                return ""

            comments = (
                thread_data[1].get("data", {}).get("children", [])
            )

            comments_text = ""
            for comment in comments[:MAX_COMMENTS_PER_THREAD]:
                c_data = comment.get("data", {})
                body = c_data.get("body", "")
                c_author = c_data.get("author", "")

                # Filter out empty comments and AutoModerator spam
                if body and c_author != "AutoModerator":
                    comments_text += f"\n- {c_author}: {body[:300]}"

            return comments_text

        except Exception as e:
            logger.debug(f"Failed to extract comments from {permalink}: {e}")
            return ""
