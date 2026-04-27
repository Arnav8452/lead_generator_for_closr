"""
Closr — Snov.io Email Finder (Free Tier: 50 searches/month)
Full OAuth2 implementation with dynamic token caching per client_id.
"""

import logging
import re
import time

import requests

from config import SCRAPER_TIMEOUT, CLOSR_TARGET_TITLES
from utils.quota_manager import quota_manager

logger = logging.getLogger("closr.enrichment.snov")

# Snov.io API endpoints
SNOV_AUTH_URL = "https://api.snov.io/v1/oauth/access_token"
SNOV_DOMAIN_SEARCH_URL = "https://api.snov.io/v2/domain-emails-with-info"
SNOV_FINDER_URL = "https://api.snov.io/v1/get-emails-from-names"

EMAIL_REGEX = re.compile(r"^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$")

# OAuth2 token cache (module-level map of client_id -> token data)
_token_cache: dict[str, dict] = {}

def _get_access_token(client_id: str, client_secret: str) -> str | None:
    """
    Obtain or refresh a Snov.io OAuth2 access token for a specific client_id.
    """
    if not client_id or not client_secret:
        return None

    now = time.time()
    token_data = _token_cache.get(client_id, {"access_token": None, "expires_at": 0})

    if token_data["access_token"] and now < token_data["expires_at"]:
        return token_data["access_token"]

    try:
        res = requests.post(
            SNOV_AUTH_URL,
            data={
                "grant_type": "client_credentials",
                "client_id": client_id,
                "client_secret": client_secret,
            },
            timeout=SCRAPER_TIMEOUT,
        )
        res.raise_for_status()
        data = res.json()

        _token_cache[client_id] = {
            "access_token": data.get("access_token"),
            "expires_at": now + data.get("expires_in", 3600) - 60,
        }
        return _token_cache[client_id]["access_token"]

    except Exception as e:
        logger.error(f"Snov auth failed for client {client_id[:8]}: {e}")
        return None


def snov_title_search(domain: str, segment: str, client_id: str, client_secret: str) -> dict | None:
    token = _get_access_token(client_id, client_secret)
    if not token or not domain:
        return None

    try:
        response = requests.get(
            SNOV_DOMAIN_SEARCH_URL,
            headers={"Authorization": f"Bearer {token}"},
            params={
                "domain": domain,
                "type": "personal",
                "limit": 50,
            },
            timeout=SCRAPER_TIMEOUT,
        )
        response.raise_for_status()
        data = response.json()

        emails = data.get("emails", [])
        if not emails:
            return None

        # Snov charges 1 credit when a domain search successfully returns emails
        quota_manager.consume_snov(client_id)

        target_titles = CLOSR_TARGET_TITLES
        
        for entry in emails:
            pos = entry.get("position", "").lower()
            email = entry.get("email")
            status = entry.get("status", "").lower()
            
            if not email or not EMAIL_REGEX.match(email):
                continue

            if any(t in pos for t in target_titles):
                if status in ("valid", "verified", "catch-all"):
                    return {
                        "verified": status in ("valid", "verified"),
                        "email": email,
                        "confidence": 100 if status in ("valid", "verified") else 75,
                        "name": f"{entry.get('firstName', '')} {entry.get('lastName', '')}".strip(),
                        "title": entry.get("position", ""),
                        "source": "snov_title",
                    }
                    
        return None
    except requests.exceptions.RequestException:
        return None


def snov_named_lookup(domain: str, first: str, last: str, client_id: str, client_secret: str) -> dict | None:
    token = _get_access_token(client_id, client_secret)
    if not token or not first or not domain:
        return None

    try:
        response = requests.post(
            SNOV_FINDER_URL,
            headers={"Authorization": f"Bearer {token}"},
            data={
                "domain": domain,
                "firstName": first,
                "lastName": last,
            },
            timeout=SCRAPER_TIMEOUT,
        )
        response.raise_for_status()
        data = response.json()
        
        emails = data.get("data", {}).get("emails", [])
        if not emails:
            return None
            
        # Snov charges 1 credit when the named search returns a valid/catch-all email
        has_valid_email = any(e.get("emailStatus", "").lower() in ("valid", "verified", "catch-all") for e in emails)
        if has_valid_email:
            quota_manager.consume_snov(client_id)
            
        for entry in emails:
            email = entry.get("email")
            status = entry.get("emailStatus", "").lower()
            
            if email and EMAIL_REGEX.match(email):
                if status in ("valid", "verified", "catch-all"):
                    return {
                        "verified": status in ("valid", "verified"),
                        "email": email,
                        "confidence": 100 if status in ("valid", "verified") else 75,
                        "name": f"{first} {last}".strip(),
                        "title": "Unknown",
                        "source": "snov_named",
                    }
                    
        return None
        
    except requests.exceptions.RequestException:
        return None