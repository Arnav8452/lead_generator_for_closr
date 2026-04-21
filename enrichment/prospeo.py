"""
Closr — Prospeo Email Finder (v2 APIs)
Uses the new 2-step Prospeo search-person + enrich-person flow.
"""

import logging
import requests

from config import PROSPEO_API_KEY, SCRAPER_TIMEOUT

logger = logging.getLogger("closr.enrichment.prospeo")

# Prospeo v2 Endpoints
SEARCH_PERSON_URL = "https://api.prospeo.io/search-person"
ENRICH_PERSON_URL = "https://api.prospeo.io/enrich-person"
EMAIL_FINDER_URL = "https://api.prospeo.io/email-finder"


def _get_target_titles(segment: str) -> list[str]:
    try:
        from enrichment.enricher import CLOSR_TARGET_TITLES
        return CLOSR_TARGET_TITLES.get(segment, [])
    except ImportError:
        return []

def prospeo_title_search(domain: str, segment: str, api_key: str) -> dict | None:
    if not api_key:
        return None


    headers = {
        "X-KEY": api_key,
        "Content-Type": "application/json"
    }

    try:
        # Step 1: Search the domain
        search_res = requests.post(
            SEARCH_PERSON_URL,
            headers=headers,
            json={
                "page": 1,
                "filters": {
                    "company": {"websites": {"include": [domain]}},
                    "person_contact_details": {
                        "email": ["VERIFIED", "CATCH_ALL"],
                        "operator": "OR"
                    }
                }
            },
            timeout=SCRAPER_TIMEOUT
        )
        search_res.raise_for_status()

        results = search_res.json().get("results", [])
        if not results:
            return None

        target_titles = _get_target_titles(segment)
        best_person_id = None
        best_title = "Unknown"
        best_name = "Unknown"

        for result in results:
            p_data = result.get("person", {})
            title = (p_data.get("job_title") or "").lower()
            if any(t.lower() in title for t in target_titles):
                best_person_id = p_data.get("person_id")
                best_title = title
                first = p_data.get("first_name", "")
                last = p_data.get("last_name", "")
                best_name = f"{first} {last}".strip()
                break
                
        if not best_person_id:
            return None

        # Step 2: Enrich the person
        enrich_res = requests.post(
            ENRICH_PERSON_URL,
            headers=headers,
            json={"only_verified_email": False, "data": {"person_id": best_person_id}},
            timeout=SCRAPER_TIMEOUT
        )
        enrich_res.raise_for_status()

        enrich_data = enrich_res.json()
        email_obj = enrich_data.get("person", {}).get("email")
        
        email = None
        if isinstance(email_obj, dict):
            email = email_obj.get("email")
        elif isinstance(email_obj, str):
            email = email_obj

        if email:
            return {
                "verified": True,
                "email": email,
                "name": best_name,
                "title": best_title,
                "source": "prospeo_title",
            }
        return None

    except requests.exceptions.RequestException:
        return None
