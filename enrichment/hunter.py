"""
Closr — Hunter.io Email Finder (Free Tier: 25 searches/month)
Uses the Hunter.io domain search API to find decision-maker emails.
"""

import logging
import re
import urllib.parse
from curl_cffi import requests
from config import SCRAPER_TIMEOUT

logger = logging.getLogger("closr.enrichment.hunter")

HUNTER_SEARCH_URL = "https://api.hunter.io/v2/domain-search"
HUNTER_FINDER_URL = "https://api.hunter.io/v2/email-finder"
EMAIL_REGEX = re.compile(r"^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$")

def _get_target_titles(segment: str) -> list[str]:
    # Import locally to avoid circular import since enricher.py defines this
    try:
        from enrichment.enricher import CLOSR_TARGET_TITLES
        return CLOSR_TARGET_TITLES.get(segment, [])
    except ImportError:
        return []

def hunter_title_search(domain: str, segment: str, api_key: str, force_department: str = None) -> dict | None:
    if not api_key:
        return None

    try:
        response = requests.get(
            HUNTER_SEARCH_URL,
            params={
                "domain": domain,
                "department": force_department if force_department else "marketing,executive,management",
                "seniority": "senior,executive",
                "type": "personal",
                "limit": 10,
                "api_key": api_key,
            },
            timeout=SCRAPER_TIMEOUT,
        )
        response.raise_for_status()
        data = response.json()

        emails = data.get("emails", [])
        if not emails:
            return None

        target_titles = _get_target_titles(segment)
        best_match = None

        for entry in emails:
            email = entry.get("value", "")
            confidence = entry.get("confidence", 0)
            position = (entry.get("position") or "").lower()
            first = entry.get("first_name", "")
            last = entry.get("last_name", "")

            if not EMAIL_REGEX.match(email) or confidence < 50:
                continue

            # Title matching logic
            if any(t.lower() in position for t in target_titles):
                best_match = {
                    "verified": entry.get("verification", {}).get("status") in ["valid", "accept_all"],
                    "email": email,
                    "name": f"{first} {last}".strip(),
                    "title": position,
                    "confidence": confidence,
                    "source": "hunter_domain",
                }
                # If we explicitly found a validated target title, stop instantly.
                if best_match["verified"]:
                    break

        return best_match

    except requests.exceptions.RequestException as e:
        return None


def hunter_named_lookup(domain: str, first: str, last: str, api_key: str) -> dict | None:
    """
    Very precise check if we know the exact name.
    """
    if not api_key or not first or not domain:
        return None

    try:
        response = requests.get(
            HUNTER_FINDER_URL,
            params={
                "domain": domain,
                "first_name": first_name,
                "last_name": last_name,
                "api_key": api_key,
            },
            timeout=SCRAPER_TIMEOUT,
        )
        response.raise_for_status()
        data = response.json()
        
        email_data = data.get("data", {})
        email = email_data.get("email")
        if email and EMAIL_REGEX.match(email):
            return {
                "verified": email_data.get("score", 0) >= 80,
                "email": email,
                "name": f"{first_name} {last_name}".strip(),
                "title": email_data.get("position", "Unknown"),
                "source": "hunter_named",
            }
            
        return None
    except requests.exceptions.RequestException:
        return None
