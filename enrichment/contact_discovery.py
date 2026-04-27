"""
Closr — Secondary Discovery Engine (Serper.dev JSON)
When unstructured news text yields zero contacts or no domain,
we hunt for them autonomously via targeted Google Searches via Serper.

This decouples Signal Extraction from Contact Discovery:
- Signal comes from news/ATS → LLM extracts company + signal
- Contacts come from HERE → targeted LinkedIn profile search
- Domain comes from HERE → targeted "official site" search
"""

import logging
import re
from urllib.parse import urlparse

from curl_cffi import requests
from config import CLOSR_TARGET_TITLES
from utils.quota_manager import quota_manager

logger = logging.getLogger("closr.enrichment.contact_discovery")

LINKEDIN_PROFILE_REGEX = re.compile(
    r'https?://(?:www\.)?linkedin\.com/in/([a-zA-Z0-9\-_%]+)/?'
)

# Domains that are never a company's actual website
SKIP_DOMAINS = {
    "linkedin.com", "facebook.com", "twitter.com", "x.com",
    "instagram.com", "youtube.com", "tiktok.com", "pinterest.com",
    "crunchbase.com", "pitchbook.com", "glassdoor.com",
    "indeed.com", "wikipedia.org", "bloomberg.com",
    "google.com", "bing.com", "reddit.com", "github.com",
    "techcrunch.com", "forbes.com", "reuters.com",
    "thesaasnews.com", "siliconangle.com", "prnewswire.com",
    "businesswire.com",
}

def _execute_search(query: str, num: int = 5, restrict_to_linkedin: bool = False) -> list[dict]:
    """
    Executes a web search. 
    If restrict_to_linkedin=True: 1st Priority Google Custom Search, 2nd Priority Serper.
    If restrict_to_linkedin=False: Direct to Serper API.
    Returns a list of dicts with keys: 'title', 'link', 'snippet'.
    """
    final_query = query
    if restrict_to_linkedin and "site:linkedin.com" not in final_query.lower():
        final_query = f"{final_query} site:linkedin.com/in"

    # ── Attempt 1: Google Custom Search (Only for LinkedIn Targets) ───────
    if restrict_to_linkedin:
        google_creds = quota_manager.get_google_search_credentials()
        if google_creds:
            api_key, cx = google_creds
            try:
                quota_manager.consume_google_search(api_key)
                res = requests.get(
                    "https://customsearch.googleapis.com/customsearch/v1",
                    params={"key": api_key, "cx": cx, "q": final_query, "num": min(num, 10)},
                    timeout=10
                )
                res.raise_for_status()
                items = res.json().get("items", [])
                return [
                    {
                        "title": r.get("title", ""),
                        "link": r.get("link", ""),
                        "snippet": r.get("snippet", "")
                    }
                    for r in items
                ]
            except Exception as e:
                logger.error(f"Google Search error: {e}. Falling back to Serper.")
                # Fall through to Serper

    # ── Attempt 2: Serper.dev Fallback or General Web Search ───────────────
    active_serper_key = quota_manager.get_serper_key()
    if not active_serper_key:
        logger.warning("All OSINT Search keys exhausted (Google & Serper).")
        return []

    try:
        quota_manager.consume_serper(active_serper_key)
        res = requests.post(
            "https://google.serper.dev/search",
            headers={"X-API-KEY": active_serper_key, "Content-Type": "application/json"},
            json={"q": final_query, "num": num},
            timeout=10
        )
        res.raise_for_status()
        organic = res.json().get("organic", [])
        return [
            {
                "title": r.get("title", ""),
                "link": r.get("link", ""),
                "snippet": r.get("snippet", "")
            }
            for r in organic
        ]
    except Exception as e:
        logger.error(f"Serper Search error: {e}")
        return []


def discover_domain(brand_name: str, niche: str = None) -> str | None:
    if not brand_name:
        return None

    # Remove quotes to allow natural semantic matching
    query = f"{brand_name} {niche} company" if niche else f"{brand_name} company"

    results = _execute_search(query, num=3, restrict_to_linkedin=False)
    for result in results:
        real_url = result.get("link", "")
        domain = _extract_root_domain(real_url)
        if domain and not any(skip in domain for skip in SKIP_DOMAINS):
            return domain
    return None


def extract_title_from_headline(headline: str, company_name: str) -> str | None:
    # Strip the name portion if present (usually before first - or |)
    # Then extract the role segment
    separators = r'[-|•·]'
    parts = [p.strip() for p in re.split(separators, headline)]
    
    role_indicators = [
        "director", "manager", "head of", "vp", "vice president",
        "chief", "cmo", "ceo", "founder", "president", "lead",
        "coordinator", "strategist", "specialist", "officer", "partner"
    ]
    
    for part in parts:
        if any(r in part.lower() for r in role_indicators):
            # Strip company name if it bled in
            if company_name:
                cleaned = re.sub(re.escape(company_name), '', part, flags=re.IGNORECASE).strip()
                # If removing company name leaves it empty or just symbols, fallback
                if len(re.sub(r'[^a-zA-Z]', '', cleaned)) > 2:
                    return cleaned
            return part
    
    return None  # couldn't extract a valid role


def discover_decision_makers(brand_name: str, niche: str = None) -> list[dict]:
    """
    Finds likely decision-makers for a given company via LinkedIn search.
    """
    if not brand_name:
        return []

    # Build an OR-heavy query targeting marketing/founder roles
    titles_query = " OR ".join(f'"{t}"' for t in CLOSR_TARGET_TITLES[:5])
    query = f'"{brand_name}" ({titles_query})'

    results = _execute_search(query, num=5, restrict_to_linkedin=True)
    contacts = []
    
    for result in results:
        title_text = result.get("title", "")
        link = result.get("link", "")

        # Verify it's an actual profile
        if not LINKEDIN_PROFILE_REGEX.match(link):
            continue

        title_text = title_text.replace(" | LinkedIn", "").replace(" - LinkedIn", "")

        separators = r'[-|•·]'
        parts = [p.strip() for p in re.split(separators, title_text)]
        
        if parts:
            name = parts[0]
            if "LinkedIn" in name or "Profiles" in name:
                continue
            name = name.split(",")[0].strip()
            
            # Use our robust title extractor
            extracted_title = extract_title_from_headline(title_text, brand_name)
            title = extracted_title if extracted_title else "Unknown"
            
            contacts.append({
                "name": name,
                "title": title,
                "linkedin_url": link,
                "confidence": "inferred_search"
            })

    return contacts[:3]  # Return top 3 matches


def _extract_root_domain(url: str) -> str | None:
    """
    Extract the root domain from a URL.
    'https://www.anthropic.com/research/paper' → 'anthropic.com'
    """
    try:
        parsed = urlparse(url)
        domain = parsed.netloc.lower()
        if not domain:
            return None

        # Strip www prefix
        domain = re.sub(r'^www\.', '', domain)
        return domain
    except Exception:
        return None