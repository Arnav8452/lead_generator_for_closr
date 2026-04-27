"""
Closr — Hunter.io Email Finder (Free Tier: 50 searches/month)
Uses the Hunter.io domain search API to find decision-maker emails.
"""

import logging
import re
import urllib.parse
from curl_cffi import requests
from config import SCRAPER_TIMEOUT, CLOSR_TARGET_TITLES
from utils.quota_manager import quota_manager

logger = logging.getLogger("closr.enrichment.hunter")

HUNTER_SEARCH_URL = "https://api.hunter.io/v2/domain-search"
HUNTER_FINDER_URL = "https://api.hunter.io/v2/email-finder"
EMAIL_REGEX = re.compile(r"^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$")

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

        # Track consumption: Hunter charges 1 request if it successfully returns emails
        quota_manager.consume_hunter(api_key)

        target_titles = CLOSR_TARGET_TITLES
        
        for entry in emails:
            pos = entry.get("position", "").lower()
            email = entry.get("value")
            confidence = entry.get("confidence", 0)
            
            if not email or not EMAIL_REGEX.match(email):
                continue

            if any(t in pos for t in target_titles):
                # Mirroring the UI by accepting anything with a high confidence score
                if confidence >= 75:
                    return {
                        "verified": confidence >= 90,
                        "email": email,
                        "confidence": confidence,
                        "name": f"{entry.get('first_name', '')} {entry.get('last_name', '')}".strip(),
                        "title": entry.get("position", "Unknown"),
                        "source": "hunter_title",
                    }
                    
        return None
    except requests.exceptions.RequestException as e:
        logger.debug(f"Hunter title search error for {domain}: {e}")
        return None

def hunter_named_lookup(domain: str, first: str, last: str, api_key: str, company: str = None) -> dict | None:
    """
    Passing the company name alongside the domain significantly improves Hunter's internal match rate.
    """
    if not api_key or not first or not domain:
        return None

    try:
        params = {
            "domain": domain,
            "first_name": first,
            "last_name": last,
            "api_key": api_key,
        }
        if company:
            params["company"] = company

        response = requests.get(
            HUNTER_FINDER_URL,
            params=params,
            timeout=SCRAPER_TIMEOUT,
        )
        response.raise_for_status()
        data = response.json()
        
        email_data = data.get("data", {})
        email = email_data.get("email")
        status = email_data.get("status")  # 'valid', 'invalid', 'accept_all', or 'webmail'
        score = email_data.get("score", 0)

        # Track consumption: Hunter charges 1 request only if an email is found
        if email:
            quota_manager.consume_hunter(api_key)

            if EMAIL_REGEX.match(email):
                # THE CRITICAL FIX: Do not demand status == 'valid'.
                # Mirror the UI by accepting anything with a high confidence score.
                if status == "valid" or score >= 75:
                    return {
                        "verified": status == "valid",
                        "email": email,
                        "confidence": score,
                        "name": f"{first} {last}".strip(),
                        "title": email_data.get("position", "Unknown"),
                        "source": "hunter_named",
                    }

        return None
    except requests.exceptions.RequestException as e:
        logger.debug(f"Hunter named lookup error for {first} {last} at {domain}: {e}")
        return None