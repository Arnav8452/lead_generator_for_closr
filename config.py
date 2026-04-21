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

HUNTER_API_KEY: str = os.getenv("HUNTER_API_KEY", "")
SNOV_CLIENT_ID: str = os.getenv("SNOV_CLIENT_ID", "")
SNOV_CLIENT_SECRET: str = os.getenv("SNOV_CLIENT_SECRET", "")
PROSPEO_API_KEY: str = os.getenv("PROSPEO_API_KEY", "")

SUPABASE_URL: str = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY: str = os.getenv("SUPABASE_KEY", "")

OLLAMA_BASE_URL: str = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
OLLAMA_MODEL: str = os.getenv("OLLAMA_MODEL", "qwen2.5:7b")

LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO")
ENVIRONMENT: str = os.getenv("ENVIRONMENT", "production")
TIMEZONE: str = os.getenv("TIMEZONE", "Asia/Kolkata")

# ─────────────────────────────────────────────────────────
# Tuning Constants
# ─────────────────────────────────────────────────────────
OLLAMA_TIMEOUT: int = 120  # Increased for CPU-offloaded inference on low-VRAM GPUs
OLLAMA_NUM_CTX: int = 2048  # Reduced from 4096 to fit in 4GB VRAM; sufficient for lead extraction
OLLAMA_TEMPERATURE: float = 0.05
SCRAPER_TIMEOUT: int = 15
SCRAPER_MAX_RETRIES: int = 3
LEAD_CONFIDENCE_THRESHOLD: float = 0.6
MAX_BRAND_NAME_LENGTH: int = 40
META_ADS_SPIKE_THRESHOLD: int = 15
JOBSPY_RESULTS_WANTED: int = 20
JOBSPY_HOURS_OLD: int = 24
HUNTER_MONTHLY_LIMIT: int = 25
SNOV_MONTHLY_LIMIT: int = 50
PROSPEO_MONTHLY_LIMIT: int = 75

# ─────────────────────────────────────────────────────────
# Enterprise Blocklist — brands too large to cold-email
# ─────────────────────────────────────────────────────────
ENTERPRISE_BLOCKLIST: list[str] = [
    "tiktok", "meta", "google", "amazon", "apple", "microsoft",
    "netflix", "disney", "nike", "coca-cola", "pepsi", "samsung",
    "walmart", "target", "unilever", "procter", "loreal", "lvmh",
]


def validate_config() -> None:
    """
    Validates that the minimum required environment variables are set.
    Exits hard on missing critical vars; prints warnings for optional ones.
    """
    errors: list[str] = []

    # ── Critical: Supabase ──────────────────────────────────
    if not SUPABASE_URL:
        errors.append("SUPABASE_URL is required but not set.")
    if not SUPABASE_KEY:
        errors.append("SUPABASE_KEY is required but not set.")

    # ── Critical: Ollama ────────────────────────────────────
    if not OLLAMA_BASE_URL:
        errors.append("OLLAMA_BASE_URL is required but not set.")

    # ── At least one enricher must be available ─────────────
    enrichers_available = any([HUNTER_API_KEY, SNOV_CLIENT_ID, PROSPEO_API_KEY])
    if not enrichers_available:
        errors.append(
            "At least ONE enrichment API key must be set: "
            "HUNTER_API_KEY, SNOV_CLIENT_ID, or PROSPEO_API_KEY."
        )

    # ── Abort on critical errors ────────────────────────────
    if errors:
        for err in errors:
            logger.critical(err)
        sys.exit(1)

    # ── Warnings (non-fatal) ────────────────────────────────
    if "3b" in OLLAMA_MODEL.lower():
        logger.warning(
            "⚠️  OLLAMA_MODEL contains '3b'. Sub-7B models have significantly "
            "lower JSON extraction reliability. Expect more validation failures."
        )

    if not META_ACCESS_TOKEN:
        logger.warning(
            "⚠️  META_ACCESS_TOKEN is not set. The Meta Ad Library scraper "
            "will be disabled for this run."
        )

    logger.info("✅ Configuration validated successfully.")
