"""
Closr — Snov.io Email Finder (Free Tier: 50 searches/month)
Full OAuth2 implementation with token caching and domain email search.
"""

import logging
import re
import time

import requests

from config import (
    SNOV_CLIENT_ID,
    SNOV_CLIENT_SECRET,
    SCRAPER_TIMEOUT,
)

logger = logging.getLogger("closr.enrichment.snov")

# Snov.io API endpoints
SNOV_AUTH_URL = "https://api.snov.io/v1/oauth/access_token"
SNOV_DOMAIN_SEARCH_URL = "https://api.snov.io/v2/domain-emails-with-info"
SNOV_FINDER_URL = "https://api.snov.io/v1/get-emails-from-names"

# Email format validator
EMAIL_REGEX = re.compile(r"^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$")

# OAuth2 token cache (module-level singleton)
_token_cache: dict = {
    "access_token": None,
    "expires_at": 0,
}


def _get_access_token() -> str | None:
    """
    Obtain or refresh a Snov.io OAuth2 access token.
    Tokens are cached in-memory and refreshed 60s before expiry.
    """
    if not SNOV_CLIENT_ID or not SNOV_CLIENT_SECRET:
        logger.debug("Snov: No client credentials configured — skipping.")
        return None

    # Return cached token if still valid (with 60s buffer)
    if (_token_cache["access_token"]
            and time.time() < _token_cache["expires_at"] - 60):
        return _token_cache["access_token"]

    try:
        response = requests.post(
            SNOV_AUTH_URL,
            data={
                "grant_type": "client_credentials",
                "client_id": SNOV_CLIENT_ID,
                "client_secret": SNOV_CLIENT_SECRET,
            },
            timeout=SCRAPER_TIMEOUT,
        )
        response.raise_for_status()
        data = response.json()

        access_token = data.get("access_token")
        expires_in = data.get("expires_in", 3600)  # Default 1hr

        if not access_token:
            logger.error("Snov: OAuth response missing access_token")
            return None

        _token_cache["access_token"] = access_token
        _token_cache["expires_at"] = time.time() + expires_in

        logger.info(f"Snov: OAuth token acquired (expires in {expires_in}s)")
        return access_token

    except requests.exceptions.RequestException as e:
        logger.error(f"Snov: OAuth token request failed: {e}")
        return None


def _get_target_titles(segment: str) -> list[str]:
    try:
        from enrichment.enricher import CLOSR_TARGET_TITLES
        return CLOSR_TARGET_TITLES.get(segment, [])
    except ImportError:
        return []

def snov_title_search(domain: str, segment: str, client_id: str, client_secret: str) -> dict | None:
    # Use global config if the passed args are None
    token = _get_access_token()
    if not token:
        return None

    try:
        url = f"{SNOV_DOMAIN_SEARCH_URL}?domain={domain}&type=all&limit=20"
        response = requests.get(
            url,
            headers={"Authorization": f"Bearer {token}"},
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
            email = entry.get("email", "")
            status = entry.get("status", "").lower()
            if not EMAIL_REGEX.match(email):
                continue
                
            position = ""
            current_jobs = entry.get("currentJobs", [])
            if current_jobs:
                position = (current_jobs[0].get("position") or "").lower()

            if any(t.lower() in position for t in target_titles):
                first_name = entry.get("firstName", "")
                last_name = entry.get("lastName", "")
                
                best_match = {
                    "verified": status in ("valid", "verified", "catch-all"),
                    "email": email,
                    "name": f"{first_name} {last_name}".strip(),
                    "title": position,
                    "source": "snov_domain",
                }
                
                # If it's explicitly verified, stop immediately.
                if best_match["verified"]:
                    break

        return best_match

    except requests.exceptions.RequestException:
        return None
