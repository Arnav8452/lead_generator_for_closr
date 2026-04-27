"""
Closr Lead Enrichment — Title-Filtered API Waterfall
"""
import requests
import time

# Seniority fallback order — stop at first match
SENIORITY_PRIORITY = ["Director", "Vice President", "C-Suite", "Founder", "Owner"]

SKIP_SEGMENTS = ["enterprise_large"]

from utils.domain_resolver import resolve_domain
from enrichment.hunter import hunter_named_lookup, hunter_title_search
from enrichment.snov import snov_named_lookup, snov_title_search
from enrichment.prospeo import prospeo_named_lookup, prospeo_title_search
from enrichment.apollo import apollo_linkedin_lookup, apollo_named_lookup as apollo_named
from config import CLOSR_TARGET_TITLES

# ── SEGMENT CLASSIFIER ─────────────────────────────────────────────────────────
def classify_segment(lead: dict) -> str:
    niche = (lead.get("niche") or "").lower()
    signal = (lead.get("intent_signal") or "").lower()
    brand = (lead.get("brand_name") or "").lower()

    large_brands = ["sony", "doordash", "victoria", "rent the runway", "sega", "twitch", "blueair", "gallo", "boeing"]
    if any(b in brand for b in large_brands):
        return "enterprise_large"

    if any(k in niche for k in ["beauty", "skincare", "dtc", "fashion", "apparel", "food", "beverage", "consumer"]):
        return "dtc_beauty"
    if any(k in niche for k in ["saas", "software", "tech", "platform"]):
        return "saas"
    if any(k in niche for k in ["fitness", "wellness", "health", "supplement"]):
        return "health_wellness"
    
    return "general"

# ── ENRICHMENT PIPELINE ────────────────────────────────────────────────────────
def enrich_lead(lead: dict, force_department: str = None, quota_manager=None) -> dict:
    """
    Waterfalls through API providers (Hunter -> Snov -> Prospeo) 
    using securely rotated keys from the QuotaManager.
    """
    domain = lead.get("domain")
    company_name = lead.get("brand_name")
    
    if not domain and company_name:
        domain = resolve_domain(company_name, niche=lead.get("niche"))
        if domain:
            lead["domain"] = domain
            
    if not domain:
        return {**lead, "contact_email": None, "reason": "no_domain"}

    segment = classify_segment(lead)
    
    if segment in SKIP_SEGMENTS:
        return {**lead, "contact_email": None, "reason": "segment_skipped"}

    result = None

    # Determine if we know exactly who we are looking for
    first = lead.get("known_first_name", "")
    last = lead.get("known_last_name", "")
    linkedin_url = lead.get("known_linkedin_url", "")

    # 0. PROSPEO LINKEDIN (highest fidelity — always try first if URL is known)
    if linkedin_url and not result and quota_manager:
        from enrichment.prospeo import prospeo_linkedin_lookup
        prospeo_key = quota_manager.get_prospeo_key()
        if prospeo_key:
            result = prospeo_linkedin_lookup(linkedin_url, prospeo_key)

    # 0b. APOLLO LINKEDIN (second highest fidelity — try if Prospeo missed)
    if linkedin_url and not result and quota_manager:
        apollo_key = quota_manager.get_apollo_key()
        if apollo_key:
            result = apollo_linkedin_lookup(linkedin_url, apollo_key)

    if first:
        # 1. STRICT NAMED SEARCH WATERFALL
        # If we know the person, do not cascade to a generic Title Search on failure.
        active_hunter_key = quota_manager.get_hunter_key() if quota_manager else None
        if not result and active_hunter_key:
            result = hunter_named_lookup(domain, first, last, active_hunter_key, company=company_name)

        if not result and quota_manager:
            snov_creds = quota_manager.get_snov_credentials()
            # Snov requires both first and last name to avoid 400 Bad Request
            if snov_creds and last:
                client_id, client_secret = snov_creds
                result = snov_named_lookup(domain, first, last, client_id, client_secret)

        # Prospeo named lookup also requires last_name
        if not result and quota_manager and last:
            prospeo_key = quota_manager.get_prospeo_key()
            if prospeo_key:
                result = prospeo_named_lookup(domain, first, last, prospeo_key, company=company_name)

        # Apollo named lookup (accepts partial last name, good fallback)
        if not result and quota_manager:
            apollo_key = quota_manager.get_apollo_key()
            if apollo_key:
                result = apollo_named(first, last or "", domain, apollo_key, company=company_name)

    else:
        # 2. TITLE SEARCH WATERFALL
        # Only run if the name is completely unknown, to avoid cross-contaminating executives.
        active_hunter_key = quota_manager.get_hunter_key() if quota_manager else None
        if not result and active_hunter_key:
            result = hunter_title_search(domain, segment, active_hunter_key, force_department=force_department)
            
        if not result and quota_manager:
            snov_creds = quota_manager.get_snov_credentials()
            if snov_creds:
                client_id, client_secret = snov_creds
                result = snov_title_search(domain, segment, client_id, client_secret)
                
        if not result and quota_manager:
            prospeo_key = quota_manager.get_prospeo_key()
            if prospeo_key:
                result = prospeo_title_search(domain, segment, prospeo_key)

    if result and result.get("email"):
        return {
            **lead,
            "contact_email": result["email"],
            "contact_name": result.get("name", ""),
            "contact_title": result.get("title", ""),
            "enrichment_source": result.get("source", "unknown"),
            "email_verified": result.get("verified", False),
            "email_confidence": result.get("confidence", 0),
            "reason": "success",
        }

    return {**lead, "contact_email": None, "reason": "no_emails_found"}