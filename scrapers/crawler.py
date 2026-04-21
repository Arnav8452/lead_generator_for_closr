import re
from typing import Set, List
from bs4 import BeautifulSoup
from curl_cffi import requests
import logging

logger = logging.getLogger(__name__)

EMAIL_REGEX = re.compile(r'\b[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z]{2,}\b')
CRAWL_PATHS = ["/", "/contact", "/about", "/team", "/people", "/founders", "/leadership", "/press", "/hello", "/reach-us"]
ROLE_BLACKLIST = {'noreply', 'no-reply', 'donotreply', 'admin', 'info', 'support', 'hello', 'contact', 'press', 'sales'}

class HybridCrawler:
    def __init__(self):
        self.headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}

    def is_js_shell(self, soup: BeautifulSoup) -> bool:
        """Detects if the page is a JS shell requiring headless rendering."""
        body = soup.find('body')
        if not body: return True
        meaningful_tags = body.find_all(['p', 'h1', 'h2', 'h3', 'li', 'span'])
        meaningful_text = sum(len(t.get_text(strip=True)) for t in meaningful_tags)
        return meaningful_text < 200

    def rank_emails(self, emails: list, domain: str) -> list:
        """Returns ONLY personal emails. Discards role-based inboxes."""
        domain_emails = [e for e in emails if domain in e.split('@')[1]]
        
        # Only keep emails that DO NOT trigger the blacklist
        personal = [e for e in domain_emails if e.split('@')[0].lower() not in ROLE_BLACKLIST]
        
        return personal # Completely dropping role-based fallback

    def extract_emails_from_html(self, html: str) -> Set[str]:
        soup = BeautifulSoup(html, "lxml")
        mailtos = {a['href'].replace('mailto:', '').split('?')[0].lower() 
                   for a in soup.find_all('a', href=True) if 'mailto:' in a['href'].lower()}
        
        for tag in soup(["script", "style", "nav", "footer", "header", "svg"]):
            tag.decompose()
            
        clean_text = soup.get_text(separator=" ", strip=True)
        text_emails = {e.lower() for e in EMAIL_REGEX.findall(clean_text)}
        return mailtos.union(text_emails)

    def crawl_domain(self, domain: str) -> str | None:
        base_url = f"https://{domain}"
        found_emails = set()

        for path in CRAWL_PATHS:
            target_url = f"{base_url}{path}"
            try:
                res = requests.get(target_url, headers=self.headers, impersonate="chrome120", timeout=8)
                if res.status_code != 200: continue
                
                soup = BeautifulSoup(res.text, "lxml")
                
                if self.is_js_shell(soup):
                    # Note: Future Playwright implementation goes here, gated by high-priority domain whitelist.
                    continue 

                found_emails.update(self.extract_emails_from_html(res.text))
                ranked = self.rank_emails(list(found_emails), domain)
                
                if ranked:
                    return ranked[0]

            except Exception as e:
                logger.debug(f"Failed to crawl {target_url}: {e}")
                
        return None
