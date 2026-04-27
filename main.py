"""
Closr — Master Orchestration Loop (V2: Micro-Batch Pipeline)
Architecture:
  Scrapers (sequential execution)
  → Extractor (Air-Lock + Sniper per raw lead)
  → LLM Queue (1 sequential worker, streams into staging list)
  → Resolution Gauntlet (RAM dedup — before ANY enrichment API calls)
  → Enrichment Workers (ReAct Harness per deduplicated entity)
  → Supabase.

LLM worker is strictly single-threaded to prevent VRAM OOM on RTX 3050.
All enrichment (Serper/Hunter/Supabase) runs concurrently via asyncio.to_thread().
"""

import os
os.environ["TOKENIZERS_PARALLELISM"] = "false"

import asyncio
import logging
import sys
import time
import re

from config import validate_config, LOG_LEVEL, META_ACCESS_TOKEN, CREATOR_ECONOMY_WHITELIST
from scrapers.google_news_funding import GoogleNewsFundingScraper
from scrapers.reddit_stealth import RedditStealthScraper
from scrapers.remote_boards import RemoteBoardsScraper
from scrapers.meta_ads import MetaAdsScraper
from scrapers.podcast_sponsors import PodcastSponsorScraper
from scrapers.ats_jobs import ATSJobsScraper
from scrapers.hacker_news import HackerNewsDistressScraper
from pipeline.llm import extract_entities
from pipeline.embedding import generate_embedding, generate_embeddings_batch

from pipeline.extractor import extractor
from pipeline.dedup import ResolutionGauntlet
from pipeline.harness import DeepResearchHarness
from pipeline.tools import normalize_title, assign_proximity_rank
from enrichment.enricher import enrich_lead
from enrichment.linkedin_resolver import resolve_linkedin, batch_resolve_linkedin
from enrichment.contact_discovery import discover_decision_makers, discover_domain
from enrichment.enricher import classify_segment
from utils.domain_resolver import resolve_domain
from utils.quota_manager import quota_manager
from db.supabase_client import (
    upsert_company,
    insert_locations,
    insert_signal,
    upsert_proximal_contact,
    update_contact_email,
    update_contact_linkedin,
    update_company_domain,
    log_pipeline_start,
    log_pipeline_finish,
    _get_client,
    get_contacts_for_company,
    insert_unresolved_email,
)

# ─────────────────────────────────────────────────────────
# Logging Setup
# ─────────────────────────────────────────────────────────
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL.upper(), logging.INFO),
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("closr.main")

EMAIL_REGEX = re.compile(r'\b[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z]{2,}\b')
ROLE_BLACKLIST = {
    'noreply', 'info', 'support', 'hello', 'contact', 'press',
    'sales', 'careers', 'getstarted', 'media', 'marketing',
}

# ── Pre-filter keywords (cheap check before expensive LLM call) ──
SIGNAL_KEYWORDS = [
    "series a", "series b", "series c", "seed", "pre-seed",
    "raises", "raised", "funding", "round", "investment", "venture",
    "million", "$",
    "hiring", "job", "role", "position", "looking for", "remote",
    "influencer", "creator", "ugc", "marketing", "growth",
    "partnership", "brand ambassador", "social media",
    "cac", "roas", "ad spend", "acquisition cost", "ad cost",
    "launch", "expand", "opening", "headquarter", "office",
]

# Concurrency tuning
ENRICHMENT_WORKERS = 5

# ─────────────────────────────────────────────────────────
# DYNAMIC CONTACT VALIDATION (The Bouncer) 
# ─────────────────────────────────────────────────────────
def is_valid_contact(name: str, title: str) -> bool:
    n = name.strip()
    t_lower = (title or "Unknown").strip().lower()
    
    # 1. Structural Check (Kills single-word Reddit usernames)
    if " " not in n:
        return False
        
    # 2. Character Check (Kills usernames like UpperLifeguard8284)
    if re.search(r'[0-9@_!#$%^&*()<>?/\|}{~:]', n):
        return False
        
    # 3. Semantic Check on Name (Kills hallucinated job titles and corporate ghosts)
    n_lower = n.lower()
    department_keywords = {
    "marketing", "sales", "growth", "founder", "director", "vp", "admin", 
    "team", "influencer", "social", "media", "support", "operations", 
    "finance", "legal", "hr", "engineering", "product", "partnerships", 
    "affiliate", "manager", "president", "chief", "executive", "office", 
    "desk", "info", "hello", "press"
    }
    parts = re.split(r'[\s,]+', n_lower)
    if not parts: return False

    # Check if the first word is a strict title/prefix
    if parts[0] in {"ceo", "cmo", "cto", "coo", "head", "lead", "president", "vp", "svp", "evp", "chief", "director"}:
        return False
    
    # Check if ANY part of the name contains a department/role keyword
    if any(keyword in parts for keyword in department_keywords):
        return False
    
    # 4. Media / Journalist Filter (Kills Non-Buyers)
    media_keywords = {
        "author", "writer", "journalist", "editor", "reporter", "contributor", 
        "news", "columnist", "correspondent", "anchor", "host", "producer", 
        "blogger", "reviewer", "publisher", "freelance", "photographer"
    }
    # Assuming t_lower is the normalized job title passed into this function
    if any(mk in t_lower for mk in media_keywords):
        return False
    
    return True

# ─────────────────────────────────────────────────────────
# Niche Gate
# ─────────────────────────────────────────────────────────
def is_lead_relevant(niche: str, summary: str, headline: str = "", signal_type: str = "") -> bool:
    text_to_check = f"{str(niche)} {str(summary)} {str(headline)}".lower()
    if any(b in text_to_check for b in CREATOR_ECONOMY_WHITELIST["blacklist"]):
        return False
    in_vertical = any(v in text_to_check for v in CREATOR_ECONOMY_WHITELIST["verticals"])
    high_intent = any(i in text_to_check for i in CREATOR_ECONOMY_WHITELIST["intent_keywords"])
    # Hiring signals with any creator/influencer mention always pass — niche may be misclassified
    if signal_type == "hiring" and high_intent:
        return True
    return in_vertical or high_intent

# ─────────────────────────────────────────────────────────
# Synchronous Processing Functions (called via to_thread)
# ─────────────────────────────────────────────────────────
def run_llm_extraction(raw_lead) -> dict | None:
    try:
        raw_lower = raw_lead.raw_text.lower()
        if not any(kw in raw_lower for kw in SIGNAL_KEYWORDS):
            logger.debug(f"  Pre-filter skip: {raw_lead.url[:60] if raw_lead.url else 'N/A'}")
            return None

        # ── V2: Air-Lock + Lexical Pulse + Sniper ───────────────
        # title: use brand_name_hint or first 120 chars of raw_text as title proxy
        title = (
            getattr(raw_lead, "brand_name_hint", None)
            or raw_lead.raw_text.split("\n")[0][:120]
        )
        surviving_chunks = extractor.filter(title=title, dom_text=raw_lead.raw_text)
        if surviving_chunks is None:
            logger.info(f"  Extractor DROP: '{title[:60]}'")
            return None
        # Feed only surviving high-density chunks to the LLM
        filtered_text = "\n\n".join(surviving_chunks)

        entity = extract_entities(filtered_text, raw_lead.source)
        if not entity: return None

        company_data = entity.get("company", {})
        company_name = company_data.get("name", "Unknown")



        niche = str(company_data.get("niche", ""))
        signal_obj = entity.get("signal", {})
        summary = str(signal_obj.get("summary", ""))
        headline = str(signal_obj.get("headline", ""))
        signal_type = str(signal_obj.get("type", ""))

        if not is_lead_relevant(niche, summary, headline, signal_type=signal_type):
            logger.info(f"  KILLSWITCH TRIGGERED: Skipping '{company_name}' (niche='{niche}', signal='{signal_type}')")
            return None

        return {
            "entity": entity,
            "raw_lead": raw_lead,
            "company_data": company_data,
            "company_name": company_name,
        }

    except Exception as e:
        brand_name = getattr(raw_lead, "brand_name_hint", "Unknown")
        logger.error(f"  LLM extraction error for {brand_name}: {e}")
        return None

def process_enrichment(extracted: dict, quota_mgr, metrics: dict) -> None:
    """
    V2 enrichment: runs the DeepResearchHarness per entity, then persists
    whatever the harness discovered to Supabase.
    Falls back to the V1 waterfall for harness failures/skips.
    """
    entity = extracted["entity"]
    raw_lead = extracted["raw_lead"]
    company_data = extracted["company_data"]
    company_name = extracted["company_name"]

    try:
        company_id = upsert_company(company_data)
        if not company_id:
            logger.warning(f"  Failed to upsert company: {company_name}")
            return
        metrics["companies_upserted"] += 1

        if not company_data.get("domain"):
            domain = resolve_domain(company_name, niche=company_data.get("niche"), source_url=getattr(raw_lead, 'url', None))
            if domain:
                company_data["domain"] = domain
                update_company_domain(company_id, domain)

        locations = entity.get("locations", [])
        for loc in locations: loc["source_url"] = raw_lead.url
        insert_locations(company_id, locations)

        signal_data = entity.get("signal", {})
        signal_data["source_url"] = raw_lead.url
        signal_data["source_name"] = raw_lead.source
        summary_text = signal_data.get("summary", "")

        signal_id = insert_signal(company_id, signal_data, embedding=None, raw_text=raw_lead.raw_text[:2000])
        if signal_id:
            metrics["signals_inserted"] += 1
            if summary_text: metrics["_pending_embeddings"].append((signal_id, summary_text))

        contacts = entity.get("contacts", [])
        contacts = [c for c in contacts if c.get("name") and str(c["name"]).strip().lower() not in ("null", "none", "n/a", "")]

        raw_expanded = []
        for contact in contacts:
            raw_name = (contact.get("name") or "").strip()
            if not raw_name: continue
            if " and " in raw_name or " & " in raw_name:
                parts = re.split(r'\s+(?:and|&)\s+', raw_name)
                for part in parts:
                    part = part.strip()
                    if part and part.lower() not in ("null", "none"):
                        new_contact = contact.copy()
                        new_contact["name"] = part
                        raw_expanded.append(new_contact)
            else:
                raw_expanded.append(contact)

        # ── CONTACT DEDUPLICATION & VALIDATION (The Bouncer + Sanitizer) ──
        expanded_contacts = []
        seen_names = {}

        for contact in raw_expanded:
            raw_name = contact.get("name", "").strip()
            raw_title = contact.get("title", "Unknown").strip()
            
            # Apply Title Sanitizer
            title = normalize_title(raw_title, company_name)
            
            # Use upgraded Bouncer (checks name AND title)
            if not is_valid_contact(raw_name, title):
                logger.debug(f"  Discarding invalid name/hallucination: {raw_name} ({title})")
                continue
                
            name_key = raw_name.lower()
            
            if name_key not in seen_names:
                contact["name"] = raw_name
                contact["title"] = title
                seen_names[name_key] = contact
                expanded_contacts.append(contact)
            else:
                existing_contact = seen_names[name_key]
                existing_title = existing_contact.get("title", "Unknown")
                if title.lower() != "unknown" and existing_title.lower() == "unknown":
                    existing_contact["title"] = title
                logger.debug(f"  Skipped duplicate LLM contact: {raw_name}")

        if not expanded_contacts:
            logger.info(f"  No contacts from LLM for '{company_name}'. Running secondary discovery...")
            discovered = discover_decision_makers(company_name, niche=company_data.get("niche"))
            for d in discovered:
                d["proximity_rank"] = assign_proximity_rank(d.get("title"))
                d["source_url"] = raw_lead.url
            expanded_contacts = discovered
        else:
            logger.info(f"  Resolving LinkedIn profiles for {len(expanded_contacts)} native contacts...")
            expanded_contacts = batch_resolve_linkedin(expanded_contacts, company_name)

        domain = company_data.get("domain")

        # Phase 1: Upsert newly scraped contacts
        for contact in expanded_contacts:
            if "proximity_rank" not in contact: contact["proximity_rank"] = assign_proximity_rank(contact.get("title"))
            if "source_url" not in contact: contact["source_url"] = raw_lead.url
            if "source_name" not in contact: contact["source_name"] = raw_lead.source

            contact_id = upsert_proximal_contact(company_id, contact)
            if contact_id:
                metrics["contacts_upserted"] += 1

        # Phase 2: Fetch all DB contacts for company and perform smart enrichment
        db_contacts = get_contacts_for_company(company_id)

        for db_contact in db_contacts:
            contact_id = db_contact.get("id")
            if not contact_id: continue

            db_name = db_contact.get("full_name", "")
            db_title = db_contact.get("job_title", "Unknown")
            db_linkedin = db_contact.get("linkedin_url")
            db_email = db_contact.get("email")
            db_rank = db_contact.get("proximity_rank", 99)

            # Re-resolve LinkedIn if missing
            if not db_linkedin:
                linkedin_url = resolve_linkedin(db_name, company_name, db_title)
                if linkedin_url:
                    update_contact_linkedin(contact_id, linkedin_url)
                    metrics["linkedin_found"] += 1
                    db_linkedin = linkedin_url

            # Enrich Email if missing
            if domain and not db_email:
                if db_rank <= 5:
                    name_parts = db_name.strip().split(" ", 1)
                    first = name_parts[0] if len(name_parts) > 0 else ""
                    last = name_parts[1] if len(name_parts) > 1 else ""

                    if not first or first.lower() in ("null", "none", "n/a"): continue

                    lead_for_enrichment = {
                        "brand_name": company_name,
                        "domain": domain,
                        "niche": company_data.get("niche"),
                        "known_first_name": first,
                        "known_last_name": last,
                        "known_linkedin_url": db_linkedin or "",
                        "company_size": company_data.get("company_size", "startup"),
                    }

                    enriched = enrich_lead(lead_for_enrichment, quota_manager=quota_mgr)
                    if enriched and enriched.get("contact_email"):
                        update_contact_email(
                            contact_id,
                            enriched["contact_email"],
                            verified=True,
                            source=enriched.get("enrichment_source", "api_waterfall"),
                        )
                        metrics["emails_found"] += 1

        # ── V2: ReAct Harness ──────────────────────────────────
        harness = DeepResearchHarness(extracted)
        harness_result = harness.run()
        harness_status = harness_result.get("status", "failed")
        harness_lead = harness_result.get("lead_data", {})

        if harness_status == "success" and harness_lead.get("email"):
            email = harness_lead["email"]
            first_name = harness_lead.get("first_name")
            last_name = harness_lead.get("last_name")
            title = harness_lead.get("title", "Unknown")

            if email and not (first_name or last_name):
                # Credit spent, name unknown — park it
                insert_unresolved_email(company_id, email, harness_lead)
                metrics["emails_found"] += 1
                logger.info(f"  ✓ Harness parked unresolved email for '{company_name}': {email}")
            elif email and (first_name or last_name):
                # Safe to upsert
                full_name = f"{first_name or ''} {last_name or ''}".strip()
                harness_contact = {
                    "name": full_name,
                    "title": title,
                    "email": email,
                    "email_verified": harness_lead.get("email_verified", False),
                    "email_source": "react_harness",
                    "proximity_rank": assign_proximity_rank(title)
                }
                new_contact_id = upsert_proximal_contact(company_id, harness_contact)
                if new_contact_id:
                    metrics["emails_found"] += 1
                    logger.info(f"  ✓ Harness found & linked email for '{company_name}': {email} to {full_name}")
        elif harness_status == "skipped":
            logger.info(f"  ↷ Harness skipped '{company_name}' (ICP score < threshold)")
        else:
            logger.debug(f"  Harness returned '{harness_status}' for '{company_name}' — no email found.")

    except Exception as e:
        brand_name = getattr(raw_lead, "brand_name_hint", company_name)
        metrics["errors"].append(f"Enrichment error for {brand_name}: {e}")
        logger.error(f"  Enrichment error for {brand_name}: {e}")

# ─────────────────────────────────────────────────────────
# Async Workers
# ─────────────────────────────────────────────────────────
async def llm_worker(
    llm_queue: asyncio.Queue,
    staging_list: list,       # V2: collect into list, not a second queue
    metrics: dict,
    seen_urls: set,
):
    while True:
        raw_lead = await llm_queue.get()
        if raw_lead is None:
            llm_queue.task_done()
            break
        try:
            lead_url = getattr(raw_lead, 'url', '') or ''
            if lead_url and lead_url in seen_urls:
                llm_queue.task_done()
                continue
            if lead_url:
                seen_urls.add(lead_url)

            extracted = await asyncio.to_thread(run_llm_extraction, raw_lead)

            if extracted:
                company_name = extracted["company_name"]
                metrics["extracted"] += 1
                staging_list.append(extracted)   # → staging for Gauntlet
                logger.info(f"  ✓ LLM extracted: {company_name} → staging")
        except Exception as e:
            logger.error(f"  LLM worker error: {e}")
        llm_queue.task_done()

async def enrichment_worker(worker_id: int, enrichment_queue: asyncio.Queue, quota_mgr, metrics: dict, max_companies: int):
    while True:
        extracted = await enrichment_queue.get()
        if extracted is None:
            enrichment_queue.task_done()
            break
        if max_companies > 0 and metrics["companies_upserted"] >= max_companies:
            enrichment_queue.task_done()
            continue
        company_name = extracted.get("company_name", "Unknown")
        logger.info(f"  [Worker-{worker_id}] Enriching: {company_name}")
        try:
            await asyncio.to_thread(process_enrichment, extracted, quota_mgr, metrics)
        except Exception as e:
            metrics["errors"].append(f"Worker-{worker_id} error: {e}")
            logger.error(f"  [Worker-{worker_id}] Error: {e}")
        enrichment_queue.task_done()

# ─────────────────────────────────────────────────────────
# Async Orchestrator
# ─────────────────────────────────────────────────────────
async def master_orchestration_loop(max_companies: int = 0) -> dict:
    start_time = time.time()
    run_id = log_pipeline_start()

    metrics = {
        "scraped": 0, "extracted": 0, "companies_upserted": 0, "signals_inserted": 0,
        "contacts_upserted": 0, "emails_found": 0, "linkedin_found": 0,
        "errors": [], "_pending_embeddings": [],
    }

    logger.info("═══ PHASE 1A: HIGH-RAM SCRAPING (Pre-Model Load) ═══")
    raw_leads = []
    
    # We fire ATS Jobs (JobSpy) BEFORE loading any AI models. 
    # This gives its Go-based concurrent threads full access to system RAM.
    ats_scraper = ATSJobsScraper()
    try:
        logger.info(f"  Starting {ats_scraper.source_name}...")
        leads = ats_scraper.run()
        raw_leads.extend(leads)
        logger.info(f"  ✓ {ats_scraper.source_name} finished: {len(leads)} raw leads")
    except Exception as e:
        error_msg = f"{ats_scraper.source_name}: {e}"
        metrics["errors"].append(error_msg)
        logger.error(f"  Scraper failed: {error_msg}")
    except BaseException as be:
        logger.critical(f"  FATAL ABORT in {ats_scraper.source_name}: {type(be).__name__}")

    # -- COLD START BLOCK --------------------------------------------------
    # Now that JobSpy is done and RAM is cleared, we load the heavy NLP models.
    logger.info("Initializing AI models (first run may take ~2 min to download)...")
    extractor.filter("warmup")   
    logger.info("Models ready. NLP-dependent scrapers launching now.")

    logger.info("═══ PHASE 1B: NLP SCRAPING (SEQUENTIAL) ═══")
    scrapers = [
        GoogleNewsFundingScraper(), 
        RedditStealthScraper(), 
        RemoteBoardsScraper(),
        PodcastSponsorScraper(), 
        HackerNewsDistressScraper(),
    ]

    if META_ACCESS_TOKEN: scrapers.append(MetaAdsScraper())
    else: logger.info("Meta Ads scraper disabled (no token)")

    # Fire the remaining scrapers sequentially
    for scraper in scrapers:
        try:
            logger.info(f"  Starting {scraper.source_name}...")
            leads = scraper.run()
            raw_leads.extend(leads)
            logger.info(f"  ✓ {scraper.source_name} finished: {len(leads)} raw leads")
        except Exception as e:
            error_msg = f"{scraper.source_name}: {e}"
            metrics["errors"].append(error_msg)
            logger.error(f"  Scraper failed: {error_msg}")
        except BaseException as be:
            logger.critical(f"  FATAL ABORT in {scraper.source_name}: {type(be).__name__}")

    metrics["scraped"] = len(raw_leads)
    logger.info(f"Total raw leads scraped: {metrics['scraped']}")

    if not raw_leads:
        logger.warning("No raw leads scraped. Pipeline ending early.")
        if run_id: log_pipeline_finish(run_id, scraped=0, extracted=0, enriched=0, injected=0, errors="No raw leads scraped", status="completed_empty")
        return metrics

    logger.info("═══ PHASE 2: LLM EXTRACTION (Extractor + Sequential Worker) ═══")
    llm_queue = asyncio.Queue()
    staging_list: list[dict] = []          # micro-batch staging before gauntlet
    seen_urls: set[str] = set()

    llm_task = asyncio.create_task(
        llm_worker(llm_queue, staging_list, metrics, seen_urls)
    )

    for raw_lead in raw_leads:
        await llm_queue.put(raw_lead)
    await llm_queue.put(None)
    await llm_task
    logger.info(f"  LLM worker complete. {metrics['extracted']} entities in staging list.")

    logger.info("═══ PHASE 2.5: RESOLUTION GAUNTLET (RAM Dedup) ═══")
    gauntlet = ResolutionGauntlet()
    deduped_entities = gauntlet.run(staging_list)
    logger.info(f"  Gauntlet complete. {len(deduped_entities)} unique entities → enrichment.")

    logger.info("═══ PHASE 3: ENRICHMENT (ReAct Harness, Concurrent Workers) ═══")
    enrichment_queue = asyncio.Queue()
    for entity in deduped_entities:
        await enrichment_queue.put(entity)

    enrichment_tasks = []
    for i in range(ENRICHMENT_WORKERS):
        task = asyncio.create_task(
            enrichment_worker(i, enrichment_queue, quota_manager, metrics, max_companies)
        )
        enrichment_tasks.append(task)

    await enrichment_queue.join()
    for _ in enrichment_tasks:
        await enrichment_queue.put(None)
    await asyncio.gather(*enrichment_tasks)
    logger.info("  All enrichment workers shut down.")

    pending_embeddings = metrics.pop("_pending_embeddings", [])
    if pending_embeddings:
        logger.info(f"═══ PHASE 3: BATCH EMBEDDING ({len(pending_embeddings)} signals) ═══")
        client = _get_client()
        texts = [summary_text for _, summary_text in pending_embeddings]
        
        try:
            embeddings = generate_embeddings_batch(texts)
            if embeddings and len(embeddings) == len(pending_embeddings):
                for (signal_id, _), emb in zip(pending_embeddings, embeddings):
                    client.table("company_signals").update({"embedding": emb}).eq("id", signal_id).execute()
            else:
                logger.error("  Batch embedding failed or returned mismatched counts.")
        except Exception as e:
            logger.error(f"  Batch embedding error: {e}")

    elapsed = time.time() - start_time
    error_summary = "; ".join(metrics["errors"]) if metrics["errors"] else None

    if run_id:
        log_pipeline_finish(
            run_id, scraped=metrics["scraped"], extracted=metrics["extracted"],
            enriched=metrics["emails_found"], injected=metrics["companies_upserted"],
            errors=error_summary, status="completed",
        )

    logger.info(
        f"═══ PIPELINE COMPLETE ═══\n  Scraped:       {metrics['scraped']}\n"
        f"  Extracted:     {metrics['extracted']}\n  Companies:     {metrics['companies_upserted']}\n"
        f"  Signals:       {metrics['signals_inserted']}\n  Contacts:      {metrics['contacts_upserted']}\n"
        f"  Emails Found:  {metrics['emails_found']}\n  LinkedIn Found:{metrics['linkedin_found']}\n"
        f"  Errors:        {len(metrics['errors'])}\n  Time:          {elapsed:.1f}s"
    )
    return metrics

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Closr Pipeline")
    parser.add_argument("--test", action="store_true", help="Test mode: process max 10 companies")
    parser.add_argument("--max", type=int, default=0, help="Max companies to process (0 = unlimited)")
    args = parser.parse_args()

    max_co = args.max if args.max > 0 else (10 if args.test else 0)
    mode_label = f"TEST ({max_co} companies)" if max_co > 0 else "FULL"

    logger.info(f"Closr — Pipeline run started (Phase 2: Async) [{mode_label}]")
    validate_config()
    result = asyncio.run(master_orchestration_loop(max_companies=max_co))
    sys.exit(0 if result["companies_upserted"] > 0 or result["scraped"] == 0 else 1)