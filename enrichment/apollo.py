"""
Closr — Apollo.io Email Enrichment

Apollo's People Enrichment API (v1):
  POST https://api.apollo.io/v1/people/match
  Headers: Content-Type: application/json, Cache-Control: no-cache
  Body: {
    api_key: str,
    first_name: str,     # optional
    last_name: str,      # optional
    name: str,           # optional (full name fallback)
    organization_name: str,  # optional
    domain: str,         # optional — company website domain
    linkedin_url: str,   # optional — highest fidelity lookup
    reveal_personal_emails: false
  }

Response: { "person": { "email": str, "email_status": str, ... } }
Free tier: 75 credits/month. Each successful match costs 1 credit.
"""

import logging
import requests

from config import SCRAPER_TIMEOUT
from utils.quota_manager import quota_manager

logger = logging.getLogger("closr.enrichment.apollo")

APOLLO_MATCH_URL = "https://api.apollo.io/v1/people/match"


def _parse_apollo_response(data: dict) -> dict | None:
    """
    Parse Apollo's /people/match response into Closr's internal format.
    Apollo returns email_status: "verified", "likely to engage", "unavailable", etc.
    """
    person = data.get("person")
    if not person:
        return None

    email = person.get("email", "")
    if not email or "@" not in email:
        return None

    status = str(person.get("email_status", "")).lower()
    is_verified = status == "verified"

    # Build full name
    first = person.get("first_name", "")
    last = person.get("last_name", "")
    full_name = f"{first} {last}".strip() or person.get("name", "Unknown")

    title = person.get("title") or person.get("headline", "Unknown")

    return {
        "email": email,
        "verified": is_verified,
        "confidence": 90 if is_verified else 65,
        "name": full_name,
        "title": title,
        "source": "apollo",
    }


def apollo_linkedin_lookup(linkedin_url: str, api_key: str) -> dict | None:
    """
    Highest-fidelity Apollo lookup — matches via LinkedIn URL.
    """
    if not api_key or not linkedin_url:
        return None

    if not quota_manager.can_use(f"apollo_{quota_manager._hash_key(api_key)}", "apollo"):
        logger.warning("Apollo quota exhausted — skipping LinkedIn lookup.")
        return None

    try:
        response = requests.post(
            APOLLO_MATCH_URL,
            headers={"Content-Type": "application/json", "Cache-Control": "no-cache"},
            json={
                "api_key": api_key,
                "linkedin_url": linkedin_url,
                "reveal_personal_emails": False,
            },
            timeout=SCRAPER_TIMEOUT,
        )
        response.raise_for_status()
        result = _parse_apollo_response(response.json())
        if result:
            quota_manager.consume_apollo(api_key)
            logger.info(f"Apollo LinkedIn hit: {result['email']} ({result['name']})")
        return result
    except requests.exceptions.RequestException as e:
        logger.debug(f"Apollo LinkedIn lookup error: {e}")
        return None


def apollo_named_lookup(
    first: str,
    last: str,
    domain: str,
    api_key: str,
    company: str = "",
) -> dict | None:
    """
    Named lookup using first + last + domain. Apollo accepts partial last names.
    """
    if not api_key or not first or not domain:
        return None

    if not quota_manager.can_use(f"apollo_{quota_manager._hash_key(api_key)}", "apollo"):
        logger.warning("Apollo quota exhausted — skipping named lookup.")
        return None

    try:
        payload = {
            "api_key": api_key,
            "first_name": first.strip(),
            "last_name": last.strip() if last else "",
            "domain": domain.strip(),
            "reveal_personal_emails": False,
        }
        if company:
            payload["organization_name"] = company.strip()

        response = requests.post(
            APOLLO_MATCH_URL,
            headers={"Content-Type": "application/json", "Cache-Control": "no-cache"},
            json=payload,
            timeout=SCRAPER_TIMEOUT,
        )
        response.raise_for_status()
        result = _parse_apollo_response(response.json())
        if result:
            quota_manager.consume_apollo(api_key)
            logger.info(f"Apollo named hit: {result['email']} ({result['name']})")
        return result
    except requests.exceptions.RequestException as e:
        logger.debug(f"Apollo named lookup error: {e}")
        return None
