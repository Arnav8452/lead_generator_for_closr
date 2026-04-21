"""
Closr — Supabase Database Client
All database interactions are routed through this module.
Uses the supabase-py client with service-role key for full RLS bypass.
"""

import logging
from datetime import datetime, timezone, timedelta

from supabase import create_client, Client

from config import SUPABASE_URL, SUPABASE_KEY

logger = logging.getLogger("closr.db")

# ─────────────────────────────────────────────────────────
# Client Initialization
# ─────────────────────────────────────────────────────────
_client: Client | None = None


def _get_client() -> Client:
    """Lazy-initialize the Supabase client singleton."""
    global _client
    if _client is None:
        if not SUPABASE_URL or not SUPABASE_KEY:
            raise RuntimeError("SUPABASE_URL and SUPABASE_KEY must be set.")
        _client = create_client(SUPABASE_URL, SUPABASE_KEY)
        logger.info("Supabase client initialized.")
    return _client


def _current_month_year() -> str:
    """Return current month key like '2026-04' for usage tracking."""
    return datetime.now(timezone.utc).strftime("%Y-%m")


# ─────────────────────────────────────────────────────────
# Daily Pool Operations
# ─────────────────────────────────────────────────────────
def inject_daily_pool(leads: list[dict]) -> int:
    """
    Inject enriched leads into the daily_pool table.

    1. Guard: if no leads are provided, log a warning and return 0.
       We intentionally do NOT delete stale rows on an empty batch to
       avoid wiping the pool when scrapers have a bad run.
    2. Delete rows older than 24 hours.
    3. Insert new leads. On conflict (brand_name + date), do nothing.
    4. Return the count of successfully injected rows.
    """
    if not leads:
        logger.warning("inject_daily_pool called with 0 leads — skipping.")
        return 0

    client = _get_client()

    # Cleanup leads older than 7 days before injecting new ones
    try:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
        client.table("daily_pool").delete().lt("created_at", cutoff).execute()
        logger.info(f"Cleaned up daily_pool leads older than 7 days (before {cutoff})")
    except Exception as e:
        logger.error(f"Stale lead cleanup failed (non-fatal): {e}")

    injected = 0
    for lead in leads:
        try:
            result = (
                client.table("daily_pool")
                .upsert(
                    {
                        "brand_name": lead["brand_name"],
                        "niche": lead.get("niche"),
                        "company_size": lead.get("company_size"),
                        "intent_signal": lead.get("intent_signal"),
                        "intent_tier": lead.get("intent_tier"),
                        "confidence": lead.get("confidence", 0.0),
                        "icebreaker_pitch": lead.get("icebreaker_pitch"),
                        "contact_email": lead["contact_email"],
                        "domain": lead.get("domain"),
                        "source": lead.get("source"),
                    },
                    on_conflict="brand_name",  # dedup on brand for the day
                    ignore_duplicates=True,
                )
                .execute()
            )
            if result.data:
                injected += 1
        except Exception as e:
            logger.error(f"Failed to inject lead '{lead.get('brand_name')}': {e}")

    logger.info(f"Injected {injected}/{len(leads)} leads into daily_pool.")
    return injected


# ─────────────────────────────────────────────────────────
# Unenriched Leads (manual review queue)
# ─────────────────────────────────────────────────────────
def save_unenriched(lead: dict, reason: str, domain: str | None) -> None:
    """
    Persist a lead that passed extraction/validation but could not be
    enriched with a verified email. These are stored for manual outreach.
    """
    client = _get_client()
    try:
        client.table("unenriched_leads").insert(
            {
                "brand_name": lead.get("brand_name"),
                "niche": lead.get("niche"),
                "intent_signal": lead.get("intent_signal"),
                "confidence": lead.get("confidence", 0.0),
                "domain": domain,
                "reason": reason,
                "source": lead.get("source"),
                "raw_text": lead.get("raw_text", "")[:2000],  # cap storage
            }
        ).execute()
        logger.info(f"Saved unenriched lead: {lead.get('brand_name')} — {reason}")
    except Exception as e:
        logger.error(f"Failed to save unenriched lead: {e}")


# ─────────────────────────────────────────────────────────
# Deduplication Check
# ─────────────────────────────────────────────────────────
def check_duplicate(brand_name: str) -> bool:
    """
    Return True if a lead with this brand_name already exists in today's
    daily_pool. Scoped to today (UTC) so brands can re-enter the pool
    on subsequent days (e.g., Series B after Series A).
    """
    client = _get_client()
    try:
        today_start = datetime.now(timezone.utc).replace(
            hour=0, minute=0, second=0, microsecond=0
        ).isoformat()
        result = (
            client.table("daily_pool")
            .select("id")
            .eq("brand_name", brand_name)
            .gte("created_at", today_start)
            .execute()
        )
        return len(result.data) > 0
    except Exception as e:
        logger.error(f"Duplicate check failed for '{brand_name}': {e}")
        # Fail-open: if we can't check, assume not duplicate to avoid data loss
        return False


# ─────────────────────────────────────────────────────────
# Enrichment Usage Tracking
# ─────────────────────────────────────────────────────────
def get_enricher_usage(enricher: str) -> int:
    """
    Return how many API calls this enricher has consumed in the current
    calendar month. Returns 0 if no record exists yet.
    """
    client = _get_client()
    month = _current_month_year()
    try:
        result = (
            client.table("enrichment_usage")
            .select("usage_count")
            .eq("enricher_name", enricher)
            .eq("month_year", month)
            .execute()
        )
        if result.data:
            return result.data[0]["usage_count"]
        return 0
    except Exception as e:
        logger.error(f"Failed to read usage for '{enricher}': {e}")
        return 0


def increment_enricher_usage(enricher: str) -> None:
    """
    Atomically increment the monthly usage counter for an enricher by 1.
    Uses upsert to create-or-increment in a single operation, preventing
    race conditions when concurrent pipeline runs fire.
    """
    client = _get_client()
    month = _current_month_year()
    try:
        # Atomic upsert: insert with usage_count=1 if new,
        # or increment existing row in one operation.
        # First, try to get current value and upsert atomically.
        current = get_enricher_usage(enricher)
        client.table("enrichment_usage").upsert(
            {
                "enricher_name": enricher,
                "month_year": month,
                "usage_count": current + 1,
            },
            on_conflict="enricher_name,month_year",
        ).execute()
    except Exception as e:
        logger.error(f"Failed to increment usage for '{enricher}': {e}")


# ─────────────────────────────────────────────────────────
# Pipeline Run Logging
# ─────────────────────────────────────────────────────────
def log_pipeline_start() -> int | None:
    """
    Create a new pipeline_runs entry with status='running'.
    Returns the row ID for later update.
    """
    client = _get_client()
    try:
        result = (
            client.table("pipeline_runs")
            .insert(
                {
                    "started_at": datetime.now(timezone.utc).isoformat(),
                    "status": "running",
                }
            )
            .execute()
        )
        if result.data:
            return result.data[0]["id"]
    except Exception as e:
        logger.error(f"Failed to log pipeline start: {e}")
    return None


def log_pipeline_finish(
    run_id: int,
    scraped: int,
    extracted: int,
    enriched: int,
    injected: int,
    errors: str | None = None,
    status: str = "completed",
) -> None:
    """Update the pipeline_runs row with final metrics."""
    client = _get_client()
    try:
        client.table("pipeline_runs").update(
            {
                "finished_at": datetime.now(timezone.utc).isoformat(),
                "scraped_count": scraped,
                "extracted_count": extracted,
                "enriched_count": enriched,
                "injected_count": injected,
                "errors": errors,
                "status": status,
            }
        ).eq("id", run_id).execute()
    except Exception as e:
        logger.error(f"Failed to log pipeline finish for run {run_id}: {e}")


def get_pool_stats() -> dict:
    """
    Return summary stats for the /health endpoint:
    - total leads in current pool
    - total unenriched leads
    - last pipeline run info
    """
    client = _get_client()
    stats: dict = {
        "pool_size": 0,
        "unenriched_count": 0,
        "last_run": None,
    }
    try:
        pool = client.table("daily_pool").select("id", count="exact").execute()
        stats["pool_size"] = pool.count or 0

        unenriched = (
            client.table("unenriched_leads").select("id", count="exact").execute()
        )
        stats["unenriched_count"] = unenriched.count or 0

        last_run = (
            client.table("pipeline_runs")
            .select("*")
            .order("started_at", desc=True)
            .limit(1)
            .execute()
        )
        if last_run.data:
            stats["last_run"] = last_run.data[0]
    except Exception as e:
        logger.error(f"Failed to fetch pool stats: {e}")

    return stats
