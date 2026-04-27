"""
Closr — LinkedIn Profile Resolver (Serper API)
Resolves Name + Title + Company → LinkedIn profile URL.

Uses Google's Serper API to guarantee JSON delivery and avoid the
CAPCTHAs triggered by legacy DuckDuckGo scraping.
"""

import logging
import re
from curl_cffi import requests

from utils.quota_manager import quota_manager

logger = logging.getLogger("closr.enrichment.linkedin_resolver")

# LinkedIn URL pattern
LINKEDIN_PROFILE_REGEX = re.compile(
    r'https?://(?:www\.)?linkedin\.com/in/[a-zA-Z0-9\-_%]+/?'
)

def resolve_linkedin(
    name: str,
    company: str,
    title: str | None = None,
) -> str | None:
    """
    Search Serper for a LinkedIn profile matching the given person.

    Args:
        name: Full name of the person (e.g., "Sarah Chen")
        company: Company name (e.g., "Glossier")
        title: Optional job title for more precise matching

    Returns:
        LinkedIn profile URL (e.g., "https://www.linkedin.com/in/sarahchen")
        or None if not found.
    """
    if not name or not company:
        return None

    active_serper_key = quota_manager.get_serper_key()
    if not active_serper_key:
        return None

    # Build search query
    query_parts = [f'site:linkedin.com/in', f'"{name}"', f'"{company}"']
    if title:
        query_parts.append(f'"{title}"')

    try:
        quota_manager.consume_serper(active_serper_key)
        res = requests.post(
            "https://google.serper.dev/search",
            headers={"X-API-KEY": active_serper_key, "Content-Type": "application/json"},
            json={"q": " ".join(query_parts), "num": 3},
            timeout=10
        )
        res.raise_for_status()
        
        results = res.json().get("organic", [])
        
        for result in results:
            linkedin_url = result.get("link", "")
            if LINKEDIN_PROFILE_REGEX.match(linkedin_url):
                # Standardize URL
                if "www.linkedin.com" not in linkedin_url:
                    linkedin_url = linkedin_url.replace(
                        "https://linkedin.com",
                        "https://www.linkedin.com"
                    ).replace(
                        "http://",
                        "https://"
                    )
                logger.debug(
                    f"LinkedIn resolver: {name} @ {company} → {linkedin_url}"
                )
                return linkedin_url

        logger.debug(
            f"LinkedIn resolver: No profile found for {name} @ {company}"
        )
        return None

    except Exception as e:
        logger.error(f"LinkedIn resolver: Error for {name} @ {company}: {e}")
        return None


def batch_resolve_linkedin(
    contacts: list[dict],
    company: str,
) -> list[dict]:
    """
    Resolve LinkedIn URLs for a batch of contacts at the same company.
    Updates each contact dict in-place with 'linkedin_url' if found.

    Args:
        contacts: List of dicts with at least 'name' and optionally 'title'.
        company: The company name.

    Returns:
        The same list with linkedin_url populated where found.
    """
    for contact in contacts:
        name = contact.get("name", "")
        title = contact.get("title")

        if not name:
            continue

        # Skip if already has a LinkedIn URL
        if contact.get("linkedin_url"):
            continue

        url = resolve_linkedin(name, company, title)
        if url:
            contact["linkedin_url"] = url

    return contacts