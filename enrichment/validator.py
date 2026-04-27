"""
Closr — V2 Email Endpoint Validator

Modern email verification without SMTP port 25 pinging.
Port 25 is dead for B2B verification in 2026 — Google Workspace and Microsoft 365
block it, route to catch-all black holes, or burn your server's IP reputation.

Verification strategy (in priority order):
  1. Prospeo /enrich-person (email field) — live delivery database, no SMTP
  2. DNS MX Record Check                  — proves domain can receive mail (not SMTP)

NOTE: Prospeo deprecated /email-verifier on March 1 2026.
The new approach is to submit the email to /enrich-person and read the email.status field.
No SMTP. No catch-all gambling. No IP reputation risk.
"""

import logging
import re
import socket

import requests

from config import SCRAPER_TIMEOUT
from utils.quota_manager import quota_manager

logger = logging.getLogger("closr.enrichment.validator")

ENRICH_PERSON_URL = "https://api.prospeo.io/enrich-person"

# Valid email format regex
_EMAIL_REGEX = re.compile(r'^[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}$')

# Prospeo v2 statuses
_VALID_STATUSES: set[str] = {"VERIFIED", "CATCH_ALL"}
_INVALID_STATUSES: set[str] = {"INVALID", "DISPOSABLE", "SPAMTRAP", "UNKNOWN"}


def _is_valid_format(email: str) -> bool:
    """Basic format check before making any API calls."""
    return bool(email and _EMAIL_REGEX.match(email.strip()))


def _has_mx_record(domain: str) -> bool:
    """
    Check if a domain has MX DNS records.
    Proves the domain is configured to receive email — no SMTP, just DNS.
    Falls back to a basic hostname lookup if dnspython isn't installed.
    """
    try:
        import dns.resolver
        answers = dns.resolver.resolve(domain, "MX", lifetime=5)
        return len(answers) > 0
    except ImportError:
        try:
            socket.getaddrinfo(domain, None, timeout=5)
            return True
        except (socket.gaierror, OSError):
            return False
    except Exception:
        return False


def _verify_via_prospeo(email: str) -> bool | None:
    """
    Verify an email using Prospeo's new /enrich-person endpoint (v2 API).
    Submits the email address as the sole data point and reads back email.status.
    Returns True (valid), False (invalid), or None (quota exhausted / API error).
    """
    key = quota_manager.get_prospeo_key()
    if not key:
        logger.debug("Validator: All Prospeo keys exhausted — skipping API check.")
        return None

    try:
        response = requests.post(
            ENRICH_PERSON_URL,
            headers={"X-KEY": key, "Content-Type": "application/json"},
            json={"only_verified_email": False, "data": {"email": email}},
            timeout=SCRAPER_TIMEOUT,
        )
        response.raise_for_status()

        data = response.json()
        # New response schema: data.person.email.status
        person = data.get("person") or {}
        email_obj = person.get("email") or {}
        status = str(email_obj.get("status", "")).upper()

        # Only consume credit if a person was actually matched
        if data.get("error") is False and person:
            quota_manager.consume_prospeo(key)

        if status in _VALID_STATUSES:
            logger.debug(f"Validator: Prospeo VALID ({status}): {email}")
            return True
        if status in _INVALID_STATUSES:
            logger.debug(f"Validator: Prospeo INVALID ({status}): {email}")
            return False

        logger.debug(f"Validator: Prospeo unknown status '{status}' for {email}")
        return None

    except requests.HTTPError as e:
        logger.warning(f"Validator: Prospeo HTTP error {e.response.status_code} for {email}")
        return None
    except Exception as e:
        logger.warning(f"Validator: Prospeo error for {email}: {e}")
        return None


def verify_email(email: str) -> bool:
    """
    Full email verification pipeline.

    1. Format check      — fail fast on obviously invalid addresses
    2. Prospeo API       — live delivery database check
    3. DNS MX fallback   — if Prospeo exhausted/unavailable

    Returns:
        True  — email is valid/deliverable
        False — email is invalid or domain has no MX records
    """
    email = (email or "").strip().lower()

    # ── Step 1: Format ───────────────────────────────────
    if not _is_valid_format(email):
        logger.debug(f"Validator: FORMAT FAIL: {email}")
        return False

    domain = email.split("@")[1]

    # ── Step 2: Prospeo ──────────────────────────────────
    prospeo_result = _verify_via_prospeo(email)
    if prospeo_result is not None:
        return prospeo_result

    # ── Step 3: DNS MX Fallback ──────────────────────────
    has_mx = _has_mx_record(domain)
    if not has_mx:
        logger.debug(f"Validator: DNS MX FAIL — {domain} has no MX records.")
        return False

    # Domain has MX records — email format is plausible
    logger.debug(f"Validator: DNS MX PASS (Prospeo unavailable): {email}")
    return True
