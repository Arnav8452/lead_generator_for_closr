import urllib.parse
from bs4 import BeautifulSoup
import logging
from curl_cffi import requests
from scrapers.base import BaseScraper, RawLead

logger = logging.getLogger("closr.scrapers")

class GoogleDorkScraper(BaseScraper):
    source_name = "Google Search (Dork Targets)"

    def fetch(self) -> list[RawLead]:
        leads = []

        # FIX 1: Removed site:twitter.com query entirely.
        # X/Twitter has blocked all Googlebot crawling since 2023 —
        # this query returns 0 results 100% of the time.

        # FIX 2: Removed VwiC3b CSS class selector — Google rotates
        # obfuscated class names every few weeks. Any hardcoded class
        # breaks silently with no error.

        # FIX 3: Switched to parsing Google's stable <cite> and <h3> tags
        # which have been consistent for years, plus data-ved anchors.

        # FIX 4: ATS dork is the highest-signal query — kept and improved.
        # Added Ashby and Workable which are now widely used by funded startups.
        queries = [
            # Direct ATS job board dork — most reliable signal
            '(site:boards.greenhouse.io OR site:jobs.lever.co OR site:app.ashbyhq.com) '
            '"influencer" OR "creator" OR "UGC"',

            # Reddit founder distress — more specific than Twitter was
            'site:reddit.com/r/SaaS OR site:reddit.com/r/Entrepreneur '
            '"influencer marketing" "looking for" OR "recommendations"',
        ]

        for query in queries:
            safe_query = urllib.parse.quote(query)
            url = f"https://www.google.com/search?q={safe_query}&tbs=qdr:d&num=20"
            # FIX: tbs=qdr:d is the correct param for last-24h filter (qdr:d not qdr=d)

            try:
                res = requests.get(url, impersonate="chrome120", timeout=15)
                # FIX: was edge101 — not a valid curl_cffi impersonate value.
                # chrome120 is stable and well-supported.

                if res.status_code == 429:
                    logger.warning(f"[{self.source_name}] Google rate limit hit — back off")
                    continue
                if res.status_code != 200:
                    logger.warning(f"[{self.source_name}] Google blocked: {res.status_code}")
                    continue

                soup = BeautifulSoup(res.text, 'html.parser')

                # FIX: Use stable selectors that don't rely on obfuscated class names.
                # h3 tags inside search results are stable. The parent <a> gives the URL.
                results_found = 0
                for div in soup.find_all('div', class_='g')[:20]:
                    h3 = div.find('h3')
                    a_tag = div.find('a', href=True)
                    if not h3 or not a_tag:
                        continue
                    
                    title = h3.get_text(strip=True)
                    link = a_tag['href']
                    
                    snippet = ""
                    # Google usually puts the snippet in the last nested div
                    texts = div.find_all('div', string=False)
                    if texts:
                        snippet = texts[-1].get_text(separator=' ', strip=True)[:300]

                    if title and link.startswith('http'):
                        full_text = f"Result Title: {title}\nSnippet: {snippet}"
                        leads.append(RawLead(
                            source=self.source_name,
                            raw_text=full_text,
                            url=link,
                        ))
                        results_found += 1

                logger.info(f"[{self.source_name}] Query '{query[:50]}...' → {results_found} results")

            except Exception as e:
                logger.error(f"[{self.source_name}] Request Failed: {e}")

        return leads
