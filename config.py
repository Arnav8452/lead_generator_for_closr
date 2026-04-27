"""
Closr — Configuration & Environment Validation
Loads all environment variables via python-dotenv and exposes them as typed constants.
Validates that critical services are reachable before the pipeline starts.
"""

import os
import sys
import logging
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger("closr.config")

# ─────────────────────────────────────────────────────────
# Service Credentials
# ─────────────────────────────────────────────────────────
META_ACCESS_TOKEN: str = os.getenv("META_ACCESS_TOKEN", "")

# Parse comma-separated keys for rotation
_hunter_keys = os.getenv("HUNTER_API_KEYS", "")
HUNTER_API_KEYS: list[str] = [k.strip(' "\'') for k in _hunter_keys.split(",")] if _hunter_keys else []

_snov_creds = os.getenv("SNOV_CREDENTIALS", "")
SNOV_CREDENTIALS: list[str] = [k.strip(' "\'') for k in _snov_creds.split(",")] if _snov_creds else []

_prospeo_keys = os.getenv("PROSPEO_API_KEYS", "")
PROSPEO_API_KEYS: list[str] = [k.strip(' "\'') for k in _prospeo_keys.split(",")] if _prospeo_keys else []

_apollo_keys = os.getenv("APOLLO_API_KEYS", "")
APOLLO_API_KEYS: list[str] = [k.strip(' "\'') for k in _apollo_keys.split(",")] if _apollo_keys else []

_serper_keys = os.getenv("SERPER_API_KEYS", "")
SERPER_API_KEYS: list[str] = [k.strip(' "\'') for k in _serper_keys.split(",")] if _serper_keys else []

_google_keys = os.getenv("GOOGLE_SEARCH_API_KEYS", "")
GOOGLE_SEARCH_API_KEYS: list[str] = [k.strip(' "\'') for k in _google_keys.split(",")] if _google_keys else []

_google_cxs = os.getenv("GOOGLE_SEARCH_CXS", "")
GOOGLE_SEARCH_CXS: list[str] = [k.strip(' "\'') for k in _google_cxs.split(",")] if _google_cxs else []

SUPABASE_URL: str = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY: str = os.getenv("SUPABASE_KEY", "")

OLLAMA_BASE_URL: str = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
OLLAMA_MODEL: str = os.getenv("OLLAMA_MODEL", "qwen2.5:7b")

# ─────────────────────────────────────────────────────────
# App & Pipeline Config
# ─────────────────────────────────────────────────────────
LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO")
ENVIRONMENT: str = os.getenv("ENVIRONMENT", "development")
TIMEZONE: str = os.getenv("TIMEZONE", "UTC")

# Concurrency & Scraping
SCRAPER_MAX_RETRIES: int = 3
SCRAPER_TIMEOUT: int = 15
DEEP_SCRAPE_ENABLED: bool = True

JOBSPY_RESULTS_WANTED: int = 30
JOBSPY_HOURS_OLD: int = 48

OLLAMA_TIMEOUT: int = 120
OLLAMA_NUM_CTX: int = 4096
OLLAMA_TEMPERATURE: float = 0.0

# ─────────────────────────────────────────────────────────
# Pipeline Tuning Parameters
# ─────────────────────────────────────────────────────────
META_ADS_SPIKE_THRESHOLD: int = 10  # Min active ads to flag a brand as high-budget
LEAD_CONFIDENCE_THRESHOLD: float = 0.80
MAX_BRAND_NAME_LENGTH: int = 40

# Strip punctuation/spaces from strings before checking this set
ENTERPRISE_BLOCKLIST: set[str] = {
    # FMCG Giants
    "unilever", "procter gamble", "pg", "johnson johnson",
    "loreal", "estee lauder", "lvmh", "kering", "nestle", 
    "pepsico", "cocacola",
    # Big Tech
    "meta", "google", "alphabet", "amazon", "apple", "microsoft",
    # Agency Conglomerates
    "wpp", "omnicom", "publicis", "dentsu", "interpublic"
}

CLOSR_TARGET_TITLES = [
    # C-Suite & VP (The Approvers)
    "founder", "co-founder", "ceo", "cmo", "vp marketing", "vp of growth",
    # Directors (The Strategists)
    "head of influencer", "director of influencer", "head of growth", 
    "creator partnerships", "head of creator", "head of affiliates",
    # Managers (The Executors - High Intent)
    "influencer marketing manager", "brand partnerships", "growth marketing manager"
]

CREATOR_ECONOMY_WHITELIST = {
    "verticals": [
        "skincare", "beauty", "dtc", "ecommerce", "e-commerce",
        "saas", "fintech", "fitness", "wellness", "edtech",
        # Added: broader creator/marketing adjacent
        "creator economy", "influencer", "marketing", "media", "content",
        "brand", "advertising", "agency", "retail", "fashion", "apparel",
        "consumer", "lifestyle", "health", "food", "beverage",
    ],
    "intent_keywords": [
        "influencer", "ugc", "creator", "ambassador", "tiktok", "partnership", "sponsorship",
        "brand deal", "content creator", "social media", "instagram", "youtube",
    ],
    "blacklist": [
        "real estate", "construction", "manufacturing", "logistics", "mining", "oil", "heavy machinery"
    ]
}

# ─────────────────────────────────────────────────────────
# Validation
# ─────────────────────────────────────────────────────────
def validate_config():
    """
    Check for critical missing variables and abort immediately.
    Log warnings for optional ones.
    """
    errors: list[str] = []

    if not SUPABASE_URL:
        errors.append("SUPABASE_URL is required but not set.")
    if not SUPABASE_KEY:
        errors.append("SUPABASE_KEY is required but not set.")

    if not OLLAMA_BASE_URL:
        errors.append("OLLAMA_BASE_URL is required but not set.")

    enrichers_available = any([HUNTER_API_KEYS, SNOV_CREDENTIALS, PROSPEO_API_KEYS])
    if not enrichers_available:
        errors.append(
            "At least ONE enrichment API key must be set: "
            "HUNTER_API_KEYS, SNOV_CREDENTIALS, or PROSPEO_API_KEYS."
        )

    if errors:
        for err in errors:
            logger.critical(err)
        sys.exit(1)

    if "3b" in OLLAMA_MODEL.lower():
        logger.warning(
            "⚠️  OLLAMA_MODEL contains '3b'. Sub-7B models have significantly "
            "lower JSON extraction reliability. Expect more validation failures."
        )

    if not META_ACCESS_TOKEN:
        logger.warning(
            "⚠️  META_ACCESS_TOKEN is not set. The Meta Ad Library scraper will skip."
        )

# ─────────────────────────────────────────────────────────
# V2: Air-Lock (bart-large-mnli Zero-Shot Classifier)
# ─────────────────────────────────────────────────────────
AIRLOCK_MODEL: str = "facebook/bart-large-mnli"
AIRLOCK_CONFIDENCE_THRESHOLD: float = 0.65

TARGET_SIGNALS: list[str] = [
    # Creator & Brand
    "sponsorship", "brand deal", "creator management", "brand ambassador", "agency of record",
    # Financial & Growth
    "funding round", "seed investment", "series A", "merger and acquisition", "market expansion",
    # Leadership & SaaS
    "executive hire", "leadership change", "strategic partnership", "B2B SaaS", "product launch",
]

# ─────────────────────────────────────────────────────────
# V2: Sniper (all-MiniLM-L6-v2 Vector Pruning)
# ─────────────────────────────────────────────────────────
SNIPER_MODEL: str = "all-MiniLM-L6-v2"
SNIPER_CHUNK_SIZE: int = 300      # words per chunk
SNIPER_CHUNK_OVERLAP: int = 50    # word overlap between chunks
SNIPER_COSINE_THRESHOLD: float = 0.35  # drop chunks below this

# ─────────────────────────────────────────────────────────
# V2: Resolution Gauntlet (RAM Deduplication)
# ─────────────────────────────────────────────────────────
GAUNTLET_JARO_THRESHOLD: float = 0.85       # Jaro-Winkler company name match
GAUNTLET_VECTOR_COSINE_THRESHOLD: float = 0.85  # Vector scalpel merge threshold

# ─────────────────────────────────────────────────────────
# V2: ReAct Harness
# ─────────────────────────────────────────────────────────
REACT_MAX_ITERATIONS: int = 5
REACT_SCORE_FIT_THRESHOLD: int = 3  # Skip enrichment if score_fit() < 3

# ─────────────────────────────────────────────────────────
# V2: Ollama JSON mode (Option A — no grammar enforcement infra change)
# ─────────────────────────────────────────────────────────
OLLAMA_FORMAT: str = "json"  # Forces Ollama to output valid JSON (no schema, but sanitizer handles the rest)