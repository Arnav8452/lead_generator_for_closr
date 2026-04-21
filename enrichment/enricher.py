"""
Closr Lead Enrichment — Title-Filtered API Waterfall
"""
import requests
import time

# ── Role targeting config by lead segment ─────────────────────────────────────
CLOSR_TARGET_TITLES = {
    "dtc_beauty": [
        "Head of Influencer", "Director of Influencer", "VP of Growth",
        "Head of Growth", "Creator Partnerships", "CMO", "VP Marketing",
        "Founder", "Co-Founder", "CEO" # FIX: Added founder titles
    ],
    "saas_funded": [
        "VP Marketing", "Head of Marketing", "VP Demand Gen", 
        "Director of Marketing", "CMO", "Head of Growth", 
        "Founder", "Co-Founder", "CEO" # FIX: Seed-stage decision makers
    ],
    "ats_signal": [
        # For ATS jobs, we still want the specific hiring manager if possible
        "Head of Influencer", "Director of Influencer", "Influencer Marketing Manager",
        "Creator Partnerships", "VP Marketing", "Founder", "CEO"
    ],
    "creator_native": [
        "Head of Creator Partnerships", "VP Creator", "Director of Creator Success",
        "Founder", "Co-Founder", "CEO"
    ],
}

# Seniority fallback order — stop at first match
SENIORITY_PRIORITY = ["Director", "Vice President", "C-Suite", "Founder", "Owner"]

SKIP_SEGMENTS = ["enterprise_large"]

from utils.domain_resolver import resolve_domain
from enrichment.hunter import hunter_named_lookup, hunter_title_search
from enrichment.snov import snov_title_search
from enrichment.prospeo import prospeo_title_search
from config import HUNTER_API_KEY, SNOV_CLIENT_ID, SNOV_CLIENT_SECRET, PROSPEO_API_KEY

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
    if any(k in niche for k in ["creator", "influencer", "media", "music", "entertainment"]):
        return "creator_native"
    if "hiring" in signal or "looking for" in signal:
        return "ats_signal"
    return "saas_funded"

# ── WATERFALL ORCHESTRATOR ────────────────────────────────────────────────────
def enrich_lead(lead: dict, config: dict = None, quota_manager=None) -> dict:
    domain = lead.get("domain")
    
    if not domain:
        return {**lead, "contact_email": None, "reason": "no_domain_resolved"}
    
    segment = classify_segment(lead)
    lead["segment"] = segment

    if segment in SKIP_SEGMENTS:
        return {**lead, "contact_email": None, "reason": "enterprise_skipped"}

    result = None
    force_department = config.get("force_department") if config else None

    # We rely on our existing global config constants instead of passing a dict
    
    # 1. Named Lookup Shortcut (Highest Accuracy)
    first = lead.get("known_first_name")
    last = lead.get("known_last_name")
    if first and last and quota_manager and quota_manager.can_use("hunter"):
        result = hunter_named_lookup(domain, first, last, HUNTER_API_KEY)
        if result and result.get("verified"):
            quota_manager.consume("hunter")

    # 2. Waterfall: Hunter → Snov → Prospeo
    if not result and quota_manager and quota_manager.can_use("hunter"):
        result = hunter_title_search(domain, segment, HUNTER_API_KEY, force_department=force_department)
        if result and result.get("verified"):
            quota_manager.consume("hunter")
            
    if not result and quota_manager and quota_manager.can_use("snov"):
        result = snov_title_search(domain, segment, SNOV_CLIENT_ID, SNOV_CLIENT_SECRET)
        if result and result.get("verified"):
            quota_manager.consume("snov")
            
    if not result and quota_manager and quota_manager.can_use("prospeo"):
        result = prospeo_title_search(domain, segment, PROSPEO_API_KEY)
        if result and result.get("verified"):
            quota_manager.consume("prospeo")

    if result and result.get("verified"):
        return {
            **lead,
            "contact_email": result["email"],
            "contact_name": result["name"],
            "contact_title": result["title"],
            "enrichment_source": result["source"],
            "reason": "success",
        }

    return {**lead, "contact_email": None, "reason": "no_verified_email_found"}
