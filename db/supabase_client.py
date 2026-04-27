"""
Closr — Supabase Database Client (Phase 1: Entity & Signal Model)
All database interactions are routed through this module.
Uses the supabase-py client with service-role key for full RLS bypass.
"""

import logging
import re
from datetime import datetime, timezone, timedelta

from supabase import create_client, Client

from config import SUPABASE_URL, SUPABASE_KEY

logger = logging.getLogger("closr.db")

# ─────────────────────────────────────────────────────────
# Company name normalization (mirrors deduplicator.py logic)
# ─────────────────────────────────────────────────────────
COMPANY_SUFFIXES = re.compile(
    r'\b(inc\.?|llc\.?|ltd\.?|co\.?|corp\.?|labs?\.?|studio|'
    r'limited|incorporated|corporation|group|holdings?|'
    r'technologies|solutions|enterprises?)\b\.?',
    re.IGNORECASE,
)


def normalize_company_name(name: str) -> str:
    """Normalize a company name for deduplication."""
    if not name:
        return ""
    normalized = name.lower().strip()
    normalized = COMPANY_SUFFIXES.sub("", normalized)
    normalized = re.sub(r'[.,]+', '', normalized)
    normalized = re.sub(r'\s+', ' ', normalized).strip()
    return normalized


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


# ═════════════════════════════════════════════════════════
# PHASE 1: Entity & Signal Model Operations
# ═════════════════════════════════════════════════════════

def upsert_company(company_data: dict) -> int | None:
    """
    Upsert a company record. Deduplicates on name_normalized.
    Returns the company ID or None on failure.

    Args:
        company_data: Dict with keys: name, niche, company_size, domain, logo_url
    """
    client = _get_client()
    name = company_data.get("name", "").strip()
    if not name:
        logger.warning("upsert_company: Empty company name — skipping.")
        return None

    name_normalized = normalize_company_name(name)
    if not name_normalized:
        return None

    try:
        result = client.table("companies").upsert(
            {
                "name": name,
                "name_normalized": name_normalized,
                "domain": company_data.get("domain"),
                "niche": company_data.get("niche"),
                "company_size": company_data.get("company_size"),
                "logo_url": company_data.get("logo_url"),
                "updated_at": datetime.now(timezone.utc).isoformat(),
            },
            on_conflict="name_normalized",
        ).execute()

        if result.data:
            company_id = result.data[0]["id"]
            logger.info(f"Upserted company: {name} (id={company_id})")
            return company_id

    except Exception as e:
        logger.error(f"Failed to upsert company '{name}': {e}")

    return None


def insert_locations(company_id: int, locations: list[dict]) -> int:
    """
    Insert location records for a company. ON CONFLICT DO NOTHING.
    Returns count of successfully inserted locations.
    """
    if not locations:
        return 0

    client = _get_client()
    inserted = 0

    for loc in locations:
        try:
            client.table("company_locations").upsert(
                {
                    "company_id": company_id,
                    "location_type": loc.get("type", "unknown"),
                    "city": loc.get("city"),
                    "region": loc.get("region"),
                    "country": loc.get("country"),
                    "raw_string": loc.get("raw"),
                    "source_url": loc.get("source_url"),
                },
                on_conflict="company_id,location_type,city,country",
                ignore_duplicates=True,
            ).execute()
            inserted += 1
        except Exception as e:
            logger.error(f"Failed to insert location for company {company_id}: {e}")

    return inserted


def insert_signal(
    company_id: int,
    signal_data: dict,
    embedding: list[float] | None = None,
    raw_text: str | None = None,
) -> int | None:
    """
    Insert a signal record with optional vector embedding.
    Returns the signal ID or None on failure.
    """
    client = _get_client()

    row = {
        "company_id": company_id,
        "signal_type": signal_data.get("type", "unknown"),
        "headline": signal_data.get("headline"),
        "summary": signal_data.get("summary"),
        "source_url": signal_data.get("source_url"),
        "source_name": signal_data.get("source_name"),
        "event_date": signal_data.get("event_date"),
        "raw_text": (raw_text or "")[:3000],
    }

    # pgvector expects the embedding as a list/array
    if embedding:
        row["embedding"] = embedding

    try:
        result = client.table("company_signals").insert(row).execute()
        if result.data:
            signal_id = result.data[0]["id"]
            logger.debug(
                f"Inserted signal for company {company_id}: "
                f"{signal_data.get('headline', 'N/A')}"
            )
            return signal_id
    except Exception as e:
        logger.error(f"Failed to insert signal for company {company_id}: {e}")

    return None


def upsert_proximal_contact(company_id: int, contact: dict) -> int | None:
    """
    Upsert a proximal contact. Deduplicates on (company_id, full_name, job_title).
    Returns the contact ID or None on failure.
    """
    client = _get_client()
    full_name = (contact.get("name") or "").strip()
    if not full_name:
        return None

    try:
        result = client.table("proximal_contacts").upsert(
            {
                "company_id": company_id,
                "full_name": full_name,
                "job_title": contact.get("title"),
                "linkedin_url": contact.get("linkedin_url"),
                "email": contact.get("email"),
                "email_verified": contact.get("email_verified", False),
                "email_source": contact.get("email_source"),
                "proximity_rank": contact.get("proximity_rank", 99),
                "source_url": contact.get("source_url"),
                "source_name": contact.get("source_name"),
                "updated_at": datetime.now(timezone.utc).isoformat(),
            },
            on_conflict="company_id,full_name,job_title",
        ).execute()

        if result.data:
            contact_id = result.data[0]["id"]
            logger.debug(f"Upserted contact: {full_name} (id={contact_id})")
            return contact_id

    except Exception as e:
        logger.error(f"Failed to upsert contact '{full_name}': {e}")

    return None


def update_contact_email(
    contact_id: int,
    email: str,
    verified: bool,
    source: str,
) -> None:
    """Update a proximal contact's email after enrichment."""
    client = _get_client()
    try:
        client.table("proximal_contacts").update(
            {
                "email": email,
                "email_verified": verified,
                "email_source": source,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
        ).eq("id", contact_id).execute()
        logger.info(f"Updated contact {contact_id} email: {email} (verified={verified})")
    except Exception as e:
        logger.error(f"Failed to update contact {contact_id} email: {e}")


def update_contact_linkedin(contact_id: int, linkedin_url: str) -> None:
    """Update a proximal contact's LinkedIn URL after resolution."""
    client = _get_client()
    try:
        client.table("proximal_contacts").update(
            {
                "linkedin_url": linkedin_url,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
        ).eq("id", contact_id).execute()
        logger.debug(f"Updated contact {contact_id} LinkedIn: {linkedin_url}")
    except Exception as e:
        logger.error(f"Failed to update contact {contact_id} LinkedIn: {e}")


def update_company_domain(company_id: int, domain: str) -> None:
    """Update a company's domain after Clearbit resolution."""
    client = _get_client()
    try:
        client.table("companies").update(
            {
                "domain": domain,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
        ).eq("id", company_id).execute()
    except Exception as e:
        logger.error(f"Failed to update company {company_id} domain: {e}")


def insert_unresolved_email(company_id: int, email: str, harness_output: dict) -> None:
    """
    Park an email discovered by the ReAct harness that lacks a clear human owner.
    """
    client = _get_client()
    try:
        # Use upsert to avoid violating the (company_id, full_name, job_title) constraint
        # if multiple unresolved emails are found for the same company over time.
        client.table("proximal_contacts").upsert(
            {
                "company_id": company_id,
                "full_name": "Unknown",
                "job_title": "Unknown",
                "email": email,
                "email_source": "react_harness_unresolved",
                "proximity_rank": 99,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            },
            on_conflict="company_id,full_name,job_title",
        ).execute()
        logger.info(f"Parked unresolved email for company {company_id}: {email}")
    except Exception as e:
        logger.error(f"Failed to park unresolved email for company {company_id}: {e}")


def get_contacts_for_company(company_id: int) -> list[dict]:
    """Fetch all proximal contacts for a company, ordered by proximity rank."""
    client = _get_client()
    try:
        result = (
            client.table("proximal_contacts")
            .select("*")
            .eq("company_id", company_id)
            .order("proximity_rank", desc=False)
            .execute()
        )
        return result.data or []
    except Exception as e:
        logger.error(f"Failed to fetch contacts for company {company_id}: {e}")
        return []


# ═════════════════════════════════════════════════════════
# Enrichment Usage Tracking (kept from Phase 0)
# ═════════════════════════════════════════════════════════

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
    """
    client = _get_client()
    month = _current_month_year()
    try:
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


# ═════════════════════════════════════════════════════════
# Pipeline Run Logging (kept from Phase 0)
# ═════════════════════════════════════════════════════════

def log_pipeline_start() -> int | None:
    """Create a new pipeline_runs entry with status='running'."""
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
    Return summary stats for the /health endpoint.
    Updated for Phase 1 schema.
    """
    client = _get_client()
    stats: dict = {
        "companies_count": 0,
        "signals_count": 0,
        "contacts_count": 0,
        "contacts_with_email": 0,
        "contacts_with_linkedin": 0,
        "last_run": None,
    }
    try:
        companies = client.table("companies").select("id", count="exact").execute()
        stats["companies_count"] = companies.count or 0

        signals = client.table("company_signals").select("id", count="exact").execute()
        stats["signals_count"] = signals.count or 0

        contacts = client.table("proximal_contacts").select("id", count="exact").execute()
        stats["contacts_count"] = contacts.count or 0

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
