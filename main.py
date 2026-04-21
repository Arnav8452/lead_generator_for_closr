"""
Closr — Master Orchestration Loop
Aggregates raw leads from all scrapers → LLM extraction → validation →
deduplication → enrichment → Supabase daily pool injection.

This is the core pipeline. Run directly for a single pass, or let
scheduler.py trigger it on a cron schedule.
"""

import logging
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from config import validate_config, LOG_LEVEL, META_ACCESS_TOKEN
from scrapers.google_news_funding import GoogleNewsFundingScraper
from scrapers.reddit_stealth import RedditStealthScraper
from scrapers.remote_boards import RemoteBoardsScraper
from scrapers.meta_ads import MetaAdsScraper
from scrapers.podcast_sponsors import PodcastSponsorScraper
from scrapers.ats_jobs import ATSJobsScraper
from scrapers.hacker_news import HackerNewsDistressScraper
from scrapers.upwork_rss import UpworkBudgetScraper
from scrapers.google_dork import GoogleDorkScraper
from pipeline.llm import extract_lead
from pipeline.validator import validate_lead
from pipeline.deduplicator import Deduplicator
from enrichment.enricher import enrich_lead
from db.supabase_client import (
    inject_daily_pool,
    save_unenriched,
    log_pipeline_start,
    log_pipeline_finish,
    _get_client,
)

import re
from scrapers.crawler import HybridCrawler
from utils.domain_resolver import resolve_domain
from utils.pattern_db import pattern_db

EMAIL_REGEX = re.compile(r'\b[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z]{2,}\b')
ROLE_BLACKLIST = {'noreply', 'info', 'support', 'hello', 'contact', 'press', 'sales', 'careers', 'getstarted', 'media', 'marketing'}

class QuotaManager:
    def __init__(self):
        self.quotas = {
            "hunter": self._fetch_quota("hunter"),
            "snov": self._fetch_quota("snov"),
            "prospeo": self._fetch_quota("prospeo")
        }
        self.limits = {"hunter": 50, "snov": 50, "prospeo": 100}

    def _fetch_quota(self, api_name: str) -> int:
        try:
            db = _get_client()
            res = db.table("enrichment_usage").select("usage_count").eq("enricher_name", api_name).execute()
            if res.data:
                return res.data[0].get("usage_count", 0)
        except Exception:
            pass
        return 0

    def can_use(self, api_name: str) -> bool:
        return self.quotas[api_name] < self.limits[api_name]

    def consume(self, api_name: str):
        self.quotas[api_name] += 1

quota_manager = QuotaManager()

# ─────────────────────────────────────────────────────────
# Logging Setup
# ─────────────────────────────────────────────────────────
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL.upper(), logging.INFO),
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("closr.main")


def master_orchestration_loop() -> dict:
    """
    Execute the full Closr pipeline:
    1. Scrape all sources for raw leads (concurrent via ThreadPoolExecutor)
    2. Extract structured data via LLM (sequential — VRAM constraint)
    3. Validate and deduplicate
    4. Enrich with verified emails
    5. Inject into Supabase daily pool
    6. Archive unenriched leads for manual review

    Returns:
        A metrics dict with counts at each stage.
    """
    start_time = time.time()
    run_id = log_pipeline_start()

    metrics = {
        "scraped": 0,
        "extracted": 0,
        "validated": 0,
        "enriched": 0,
        "injected": 0,
        "unenriched": 0,
        "errors": [],
    }

    # ── Step 1: Aggregate raw leads from all scrapers (CONCURRENT) ───
    logger.info("═══ PHASE 1: SCRAPING (CONCURRENT) ═══")
    raw_leads = []

    scrapers = [
        GoogleNewsFundingScraper(),
        RedditStealthScraper(),
        RemoteBoardsScraper(),
        ATSJobsScraper(),
        PodcastSponsorScraper(),
        HackerNewsDistressScraper(),
        GoogleDorkScraper(),
    ]

    # Only include Meta Ads if the token is configured
    if META_ACCESS_TOKEN:
        scrapers.append(MetaAdsScraper())
    else:
        logger.info("Meta Ads scraper disabled (no token)")

    # Run all scrapers concurrently — they are I/O-bound (HTTP requests).
    # The LLM phase that follows is kept strictly sequential (VRAM constraint).
    with ThreadPoolExecutor(max_workers=len(scrapers)) as executor:
        future_to_scraper = {
            executor.submit(scraper.run): scraper for scraper in scrapers
        }
        for future in as_completed(future_to_scraper):
            scraper = future_to_scraper[future]
            try:
                leads = future.result()
                raw_leads.extend(leads)
                logger.info(f"  {scraper.source_name}: {len(leads)} raw leads")
            except Exception as e:
                error_msg = f"{scraper.source_name}: {e}"
                metrics["errors"].append(error_msg)
                logger.error(f"  Scraper failed: {error_msg}")

    metrics["scraped"] = len(raw_leads)
    logger.info(f"Total raw leads scraped: {metrics['scraped']}")

    if not raw_leads:
        logger.warning("No raw leads scraped. Pipeline ending early.")
        if run_id:
            log_pipeline_finish(
                run_id,
                scraped=0, extracted=0, enriched=0, injected=0,
                errors="No raw leads scraped",
                status="completed_empty",
            )
        return metrics

    # ── Step 2-4: Sequential Processing (LLM -> Validation -> Enrichment) ──
    logger.info("═══ PHASE 2: PROCESSING (SEQUENTIAL) ═══")
    enriched_leads = []
    deduplicator = Deduplicator()
    crawler = HybridCrawler()

    for raw_lead in raw_leads:
        try:
            # Step 1: LLM EXTRACTION (Context & Pitch)
            # You pay the local token compute here to guarantee you have the brand & pitch.
            lead = extract_lead(raw_lead.raw_text, raw_lead.source)
            if not lead:
                continue
                
            lead["url"] = raw_lead.url
            if (raw_lead.brand_name_hint
                    and lead.get("confidence", 0) < 0.3
                    and not lead.get("brand_name")):
                lead["brand_name"] = raw_lead.brand_name_hint
                
            metrics["extracted"] += 1
            brand_name = lead.get("brand_name", "Unknown")

            # Validation gate
            is_valid, reason = validate_lead(lead)
            if not is_valid:
                logger.debug(f"  Rejected: {brand_name} — {reason}")
                continue

            # Deduplication gate
            if deduplicator.is_duplicate(brand_name):
                logger.debug(f"  Duplicate: {brand_name}")
                continue

            metrics["validated"] += 1

            # Step 2: REGEX SCAN RAW TEXT (With Strict Blacklist)
            emails_in_text = EMAIL_REGEX.findall(raw_lead.raw_text)
            valid_raw_email = None
            
            for email in emails_in_text:
                local_part = email.split('@')[0].lower()
                if local_part not in ROLE_BLACKLIST:
                    valid_raw_email = email.lower()
                    break # Grab the first personal email we find
            
            if valid_raw_email:
                logger.info(f"Regex found valid personal email: {valid_raw_email}. Bypassing APIs.")
                lead["contact_email"] = valid_raw_email
                lead["enrichment_source"] = "regex_raw_text"
                lead["reason"] = "success"
                enriched_leads.append(lead)
                metrics["enriched"] += 1
                logger.info(f"  ✓ {brand_name} → {lead['contact_email']}")
                continue 
            elif emails_in_text:
                logger.info(f"Regex found role-based email ({emails_in_text[0]}). Ignoring and pushing to API waterfall.")

            # Step 3: DOMAIN RESOLUTION
            domain = resolve_domain(brand_name)
            if not domain:
                reason = "no_domain_resolved"
                save_unenriched(lead, reason=reason, domain=None)
                metrics["unenriched"] += 1
                logger.info(f"  ✗ {brand_name} — unenriched ({reason})")
                continue
            lead["domain"] = domain
            
            # Step 4: Name Parsing Logic for Pattern DB & Waterfall
            name_str = lead.get("decision_maker_name")
            if name_str and str(name_str).lower() not in ["null", "none", ""]:
                parts = str(name_str).strip().split(" ", 1)
                lead["known_first_name"] = parts[0]
                lead["known_last_name"] = parts[1] if len(parts) > 1 else ""
            else:
                lead["known_first_name"] = None
                lead["known_last_name"] = None
                name_str = None

            # Step 5: PATTERN DB LOOKUP (Free, Compounding)
            cached_email = pattern_db.lookup(domain, name_str) if name_str else None
            if cached_email:
                lead["contact_email"] = cached_email
                lead["enrichment_source"] = "pattern_db"
                lead["reason"] = "success"
                enriched_leads.append(lead)
                metrics["enriched"] += 1
                logger.info(f"  ✓ {brand_name} → {lead['contact_email']}")
                continue

            # Check Company Size for Crawler & API Logic
            company_size = lead.get("company_size", "startup").lower()
            decision_maker = lead.get("decision_maker_name")

            # Step 6: HYBRID DOMAIN CRAWLER (Skip for Enterprise to save time)
            if company_size in ["enterprise", "mid_market"]:
                logger.info(f"Skipping crawler for {brand_name} (Enterprise domain too large).")
            else:
                crawled_email = crawler.crawl_domain(domain)
                if crawled_email:
                    lead["contact_email"] = crawled_email
                    lead["enrichment_source"] = "domain_crawl"
                    lead["reason"] = "success"
                    enriched_leads.append(lead)
                    metrics["enriched"] += 1
                    logger.info(f"  ✓ {brand_name} → {lead['contact_email']}")
                    continue

            # Step 7: API WATERFALL (With Department Forcing for Nameless Leads)
            logger.info(f"Routing {brand_name} to API Waterfall...")
            
            enrichment_config = {}
            if not decision_maker or decision_maker.lower() == "null":
                enrichment_config["force_department"] = "marketing,executive"
            else:
                enrichment_config["force_department"] = None

            lead = enrich_lead(lead, enrichment_config, quota_manager)
            if lead.get("contact_email"):
                enriched_leads.append(lead)
                metrics["enriched"] += 1
                logger.info(f"  ✓ {brand_name} → {lead['contact_email']}")
            else:
                reason = lead.get("reason", "no_email_found")
                save_unenriched(lead, reason=reason, domain=domain)
                metrics["unenriched"] += 1
                logger.info(f"  ✗ {brand_name} — unenriched ({reason})")

        except Exception as e:
            brand_name = getattr(raw_lead, "brand_name_hint", "Unknown")
            metrics["errors"].append(f"Processing error for {brand_name}: {e}")
            logger.error(f"  Processing error for {brand_name}: {e}")
            save_unenriched({"raw_text": raw_lead.raw_text, "brand_name": brand_name}, reason=f"processing_error: {e}", domain=None)
            metrics["unenriched"] += 1

    logger.info(
        f"Extracted: {metrics['extracted']}/{metrics['scraped']} "
    )
    logger.info(
        f"Validated: {metrics['validated']}/{metrics['extracted']} "
        f"(rejected {metrics['extracted'] - metrics['validated']})"
    )
    logger.info(
        f"Enriched: {metrics['enriched']}/{metrics['validated']} "
        f"(unenriched: {metrics['unenriched']})"
    )

    # ── Step 5: Inject into Daily Pool ──────────────────────
    logger.info("═══ PHASE 5: INJECTION ═══")
    metrics["injected"] = inject_daily_pool(enriched_leads)
    logger.info(f"Injected into daily pool: {metrics['injected']}")

    # ── Pipeline Complete ───────────────────────────────────
    elapsed = time.time() - start_time
    error_summary = "; ".join(metrics["errors"]) if metrics["errors"] else None

    if run_id:
        log_pipeline_finish(
            run_id,
            scraped=metrics["scraped"],
            extracted=metrics["extracted"],
            enriched=metrics["enriched"],
            injected=metrics["injected"],
            errors=error_summary,
            status="completed",
        )

    logger.info(
        f"═══ PIPELINE COMPLETE ═══\n"
        f"  Scraped:    {metrics['scraped']}\n"
        f"  Extracted:  {metrics['extracted']}\n"
        f"  Validated:  {metrics['validated']}\n"
        f"  Enriched:   {metrics['enriched']}\n"
        f"  Injected:   {metrics['injected']}\n"
        f"  Unenriched: {metrics['unenriched']}\n"
        f"  Errors:     {len(metrics['errors'])}\n"
        f"  Time:       {elapsed:.1f}s"
    )

    return metrics


if __name__ == "__main__":
    logger.info("Closr — Manual pipeline run started")
    validate_config()
    result = master_orchestration_loop()
    sys.exit(0 if result["injected"] > 0 or result["scraped"] == 0 else 1)
