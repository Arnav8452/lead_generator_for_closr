import xml.etree.ElementTree as ET
import re
import logging
from curl_cffi import requests
from scrapers.base import BaseScraper, RawLead

logger = logging.getLogger("closr.scrapers")

class UpworkBudgetScraper(BaseScraper):
    source_name = "Upwork RSS (Active Budgets)"

    def fetch(self) -> list[RawLead]:
        leads = []
        # FIX: Upwork's public RSS endpoint requires a valid session cookie — the
        # /ab/feed/ path 403s for unauthenticated requests. Use the legacy
        # /jobs/rss endpoint instead, which is still publicly accessible.
        # Also expanded queries to cast a wider net.
        feeds = [
            "https://www.upwork.com/jobs/rss?q=UGC+Creator&sort=recency",
            "https://www.upwork.com/jobs/rss?q=TikTok+Ads+creator&sort=recency",
            "https://www.upwork.com/jobs/rss?q=influencer+marketing&sort=recency",
            "https://www.upwork.com/jobs/rss?q=UGC+video+ads&sort=recency",
        ]

        for url in feeds:
            try:
                # FIX: chrome120 is valid; was already correct but kept for clarity
                res = requests.get(url, impersonate="chrome120", timeout=15)
                if res.status_code != 200:
                    logger.warning(f"[{self.source_name}] Feed blocked ({res.status_code}): {url}")
                    continue

                root = ET.fromstring(res.text)
                for item in root.findall('.//item')[:15]:
                    title_el = item.find('title')
                    desc_el = item.find('description')
                    link_el = item.find('link')

                    title = title_el.text or "" if title_el is not None else ""
                    description = desc_el.text or "" if desc_el is not None else ""
                    link = link_el.text or "" if link_el is not None else ""

                    # FIX: Removed "Entry level" filter entirely.
                    # Upwork encodes this in <category> not <description>,
                    # so the string match never triggered — but it also
                    # would have been too aggressive anyway. Budget is the right filter.

                    # FIX: Upwork description HTML uses "Budget: $X" but also
                    # "Hourly Range: $X-$Y" — original regex missed hourly jobs entirely.
                    # Now check both formats and treat hourly * estimated hours as budget.
                    budget_val = self._extract_budget(description)

                    # FIX: Threshold dropped from $500 to $200.
                    # Most Upwork UGC/creator jobs are $200-$800 per video.
                    # $500 minimum was filtering the majority of the market.
                    if budget_val is not None and budget_val < 200:
                        continue

                    full_text = f"Job Title: {title}\nDescription: {description}"
                    leads.append(RawLead(
                        source=self.source_name,
                        raw_text=full_text,
                        url=link,
                    ))

            except ET.ParseError as e:
                logger.error(f"[{self.source_name}] XML parse error: {e}")
            except Exception as e:
                logger.error(f"[{self.source_name}] Error: {e}")

        logger.info(f"Upwork: {len(leads)} leads found")
        return leads

    def _extract_budget(self, description: str) -> int | None:
        """
        Extract a usable budget figure from Upwork description HTML.
        Handles three formats Upwork uses:
          - Fixed: "Budget: $500"
          - Hourly: "Hourly Range: $25.00-$50.00" → take the midpoint
          - No budget listed → return None (don't filter out)
        """
        # Fixed budget
        fixed = re.search(r'Budget:\s*\$(\d[\d,]*)', description)
        if fixed:
            return int(fixed.group(1).replace(',', ''))

        # Hourly range — take lower bound as proxy
        hourly = re.search(r'Hourly Range:\s*\$([\d.]+)-\$([\d.]+)', description)
        if hourly:
            low = float(hourly.group(1))
            # Assume 10hr minimum engagement as budget proxy
            return int(low * 10)

        # No budget info — don't filter out, let LLM decide
        return None
