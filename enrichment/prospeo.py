"""
Closr — Prospeo Email Enrichment (v2 New API — March 2026)

Prospeo deprecated all old endpoints (linkedin-email-finder, email-finder, etc.)
as of March 1 2026. This module uses the new unified /enrich-person endpoint.

New API contract:
  POST https://api.prospeo.io/enrich-person
  Headers: X-KEY, Content-Type: application/json
  Body: {
    "only_verified_email": true,   # optional: only charge if email is VERIFIED
    "data": {
      # ONE of the following combinations (minimum):
      # 1. linkedin_url only
      # 2. first_name + last_name + (company_website | company_name)
      # 3. full_name + (company_website | company_name)
      ...
    }
  }

Response: { "error": false, "person": { ..., "email": { "status": "VERIFIED", "email": "..." } } }
"""

import logging
import requests

from config import SCRAPER_TIMEOUT, CLOSR_TARGET_TITLES
from utils.quota_manager import quota_manager

logger = logging.getLogger("closr.enrichment.prospeo")

ENRICH_PERSON_URL = "https://api.prospeo.io/enrich-person"
SEARCH_PERSON_URL = "https://api.prospeo.io/search-person"


def _parse_enrich_response(data: dict, source: str) -> dict | None:
    """
    Parse the unified /enrich-person response into Closr's internal format.
    """
    person = data.get("person", {})
    if not person:
        return None

    email_obj = person.get("email") or {}
    email = email_obj.get("email", "")
    status = str(email_obj.get("status", "")).upper()

    # Strip masked emails (Prospeo masks unverified as "email@*****.com")
    if not email or "*" in email:
        return None

    is_verified = status == "VERIFIED"

    return {
        "verified": is_verified,
        "email": email,
        "confidence": 100 if is_verified else 70,
        "name": person.get("full_name", "Unknown"),
        "title": person.get("current_job_title", "Unknown"),
        "source": source,
    }


def prospeo_linkedin_lookup(linkedin_url: str, api_key: str) -> dict | None:
    """
    Enrich a person from their LinkedIn URL using the new /enrich-person endpoint.
    This is the highest-fidelity Prospeo call (single datapoint is sufficient).
    """
    if not api_key or not linkedin_url:
        return None

    headers = {"X-KEY": api_key, "Content-Type": "application/json"}

    try:
        response = requests.post(
            ENRICH_PERSON_URL,
            headers=headers,
            json={
                "only_verified_email": False,  # accept unverified too, we check status
                "data": {"linkedin_url": linkedin_url}
            },
            timeout=SCRAPER_TIMEOUT,
        )
        response.raise_for_status()
        result = _parse_enrich_response(response.json(), source="prospeo_linkedin")
        if result:
            quota_manager.consume_prospeo(api_key)
        return result

    except requests.exceptions.RequestException as e:
        logger.debug(f"Prospeo LinkedIn lookup error: {e}")
        return None


def prospeo_named_lookup(domain: str, first: str, last: str, api_key: str, company: str = None) -> dict | None:
    """
    Enrich a person using first_name + last_name + company_website via /enrich-person.
    Requires both first AND last name — Prospeo will 400 without both.
    """
    if not api_key or not first or not last or not domain:
        return None

    headers = {"X-KEY": api_key, "Content-Type": "application/json"}

    payload_data = {
        "first_name": first.strip(),
        "last_name": last.strip(),
        "company_website": domain,
    }
    if company:
        payload_data["company_name"] = company.strip()

    try:
        response = requests.post(
            ENRICH_PERSON_URL,
            headers=headers,
            json={"only_verified_email": False, "data": payload_data},
            timeout=SCRAPER_TIMEOUT,
        )
        response.raise_for_status()
        result = _parse_enrich_response(response.json(), source="prospeo_named")
        if result:
            quota_manager.consume_prospeo(api_key)
        return result

    except requests.exceptions.RequestException as e:
        logger.debug(f"Prospeo named lookup error: {e}")
        return None


def prospeo_title_search(domain: str, segment: str, api_key: str) -> dict | None:
    """
    2-step: Search the domain for a person matching our ICP titles,
    then enrich the matched person_id to reveal the email.
    """
    if not api_key or not domain:
        return None

    headers = {"X-KEY": api_key, "Content-Type": "application/json"}

    try:
        # Step 1: Search by domain
        search_res = requests.post(
            SEARCH_PERSON_URL,
            headers=headers,
            json={
                "page": 1,
                "filters": {
                    "company": {"websites": {"include": [domain]}}
                }
            },
            timeout=SCRAPER_TIMEOUT,
        )
        search_res.raise_for_status()

        results = search_res.json().get("results", [])
        if not results:
            return None

        # Filter against Closr ICP target titles
        target_person = None
        for person in results:
            pos = person.get("current_job_title", "").lower()
            if any(t in pos for t in CLOSR_TARGET_TITLES):
                target_person = person
                break

        if not target_person:
            return None

        person_id = target_person.get("person_id") or target_person.get("id")
        if not person_id:
            return None

        # Step 2: Enrich the matched person_id
        enrich_res = requests.post(
            ENRICH_PERSON_URL,
            headers=headers,
            json={"only_verified_email": False, "data": {"person_id": person_id}},
            timeout=SCRAPER_TIMEOUT,
        )
        enrich_res.raise_for_status()

        result = _parse_enrich_response(enrich_res.json(), source="prospeo_title")
        if result:
            quota_manager.consume_prospeo(api_key)
        return result

    except requests.exceptions.RequestException as e:
        logger.debug(f"Prospeo title search error: {e}")
        return None