"""
Closr — LLM Extraction Pipeline (Ollama / Local)
Phase 1: Deep Semantic Extraction — extracts full entity graphs from unstructured text.

Two-stage pipeline:
  Stage A: Deep extraction via DEEP_EXTRACTION_PROMPT → structured JSON
  Stage B: Embedding generation via pipeline.embedding (separate module)

Uses Ollama's HTTP API — no external LLM costs.
"""

import json
import logging
import re

import requests

from config import (
    OLLAMA_BASE_URL,
    OLLAMA_MODEL,
    OLLAMA_TIMEOUT,
    OLLAMA_NUM_CTX,
    OLLAMA_TEMPERATURE,
)

logger = logging.getLogger("closr.pipeline.llm")

# ─────────────────────────────────────────────────────────
# Deep Extraction Prompt — Phase 1 Entity & Signal Model
# ─────────────────────────────────────────────────────────
DEEP_EXTRACTION_PROMPT = """You are the "Closr Deep Intelligence Extractor", a B2B sales intelligence engine.
You receive raw, unstructured text (news articles, press releases, job postings,
social media threads, podcast descriptions) and you MUST extract ALL structured
entities and signals from it.

You MUST output ONLY valid JSON. No explanations, no markdown fences, no commentary.

**EXTRACTION SCHEMA (JSON ONLY):**

{
    "company": {
        "name": "The PRIMARY company spending money or hiring (proper case, no Inc/LLC/Ltd suffixes)",
        "niche": "Industry vertical (e.g., skincare, SaaS, fintech, fitness, creator economy)",
        "company_size": "Best estimate: startup | small | medium | enterprise"
    },
    "signal": {
        "type": "One of: funding | hiring | expansion | product_launch | distress | ad_spend",
        "headline": "One-line summary of the event (e.g., 'Series A $12M led by Sequoia')",
        "summary": "2-3 sentence context explaining WHY this signal matters for outreach. Be specific about amounts, timelines, and strategic implications.",
        "event_date": "ISO 8601 date if mentioned or inferable (e.g., '2026-04-22'). Use null if unknown."
    },
    "locations": [
        {
            "type": "hq | hiring | office | expansion",
            "city": "City name or null",
            "region": "State/Province or null",
            "country": "Country name or null",
            "raw": "Original location string as it appeared in the text"
        }
    ],
    "contacts": [
        {
            "name": "First and Last name of ONE person. NEVER group multiple names. (e.g., 'Sarah Chen')",
            "title": "Job title exactly as stated (e.g., 'VP of Growth')",
            "context": "Why this person is relevant (e.g., 'quoted in the funding announcement', 'listed as hiring manager')"
        }
    ],
    "strategic_context": [
        "Each string is a specific, actionable insight extracted from the text.",
        "Examples: 'Expanding European operations with Berlin office opening Q3 2026'",
        "'Launching AI-powered skincare line targeting Gen Z'",
        "'Founder expressed frustration with Meta ROAS declining 40% YoY'"
    ],
    "confidence": 0.85
}

**CRITICAL RULES:**
1. `company.name` MUST be the actual brand/company — NOT a person, job board, news outlet, or platform name. Strip Inc/LLC/Ltd suffixes.
2. If no clear company can be identified, return exactly: {}
3. ONLY extract individuals who work directly for the target company in the target-department orbit (marketing, growth, creator partnerships, founders/CEOs).
4. DO NOT extract the author, journalist, reporter, or publisher of the article.
5. DO NOT extract investors, board members, or external PR contacts unless they are active founders.
6. DO NOT extract names that are just job titles (e.g., "Director of Marketing") or social media usernames (e.g., names without spaces like "User123").
7. NEVER group multiple people in a single contact object. If the text says "Founded by Dana and Shlomit", you MUST create TWO separate objects in the `contacts` array. No "and", "&", or commas in the name field. One person = one object.
8. For each location, DISTINGUISH between HQ (where they're headquartered) and hiring/expansion locations (where budget is being deployed).
9. `strategic_context` should contain insights a sales rep could use in a cold email. Be SPECIFIC — no generic statements.
10. `event_date` should be an actual date. If the text says "yesterday" or "last week", infer the approximate date. If truly unknown, use null.
11. confidence should be 0.8+ only if there is a clearly identifiable company with an actionable signal.
12. For job postings: the company is the employer, signal type is "hiring", extract the job location as a "hiring" type location.
13. For funding news: extract round type, amount, and lead investor in the headline. Extract founders/CEOs as contacts (they control the budget).
14. NEVER output anything except the JSON object.
15. REDDIT/STEALTH RULE: If the text is a Reddit post where the author is asking for advice but DOES NOT explicitly reveal their startup's name, DO NOT extract their Reddit username as the company. You MUST return exactly: {}"""

# ─────────────────────────────────────────────────────────
# Legacy Prompt (deprecated — kept for backward compat)
# ─────────────────────────────────────────────────────────
SYSTEM_PROMPT = """You are the "Closr Economic Strategist & Data Validator", functioning as a B2B sales intelligence extraction engine. Your job is to analyze raw text from various sources (press releases, job postings, social media posts, ad data, podcast show notes) and extract structured lead data.

You must output ONLY valid JSON. No explanations, no markdown, no extra text.

**1. STRICT SAFETY & EXTRACTION RULES:**
1. brand_name must be the actual consumer/SaaS brand, NOT a person's name, a job board, or a media outlet.
2. Evaluate the text. If it is a lifestyle blog, media outlet (e.g., WRAL, generic news), or irrelevant corporate announcement without a clear brand name, you MUST return exactly {}.
3. Do not hallucinate brand names or domains. Extract the exact brand explicitly mentioned. If you cannot identify a clear brand from the text, set confidence to 0.0 or return {}.
4. If the text mentions multiple brands, extract only the PRIMARY brand (the one spending money).
5. confidence should be 0.8+ only if there is a clear, actionable signal.

**2. ECONOMIC EVALUATION FRAMEWORK:**
- Economic Intent: Identify capital injections (Seed/Series A) or distressed sentiment regarding ad costs or hiring gaps.
- Marginal CAC Focus: If a brand is scaling spend (funding), assume their marginal CAC is ballooning (frequently exceeding 130% of trailing average). Position creator-led marketing as the fix.
- UGC Strategy: Advocate for "low-fidelity, unpolished UGC" as an authenticity proxy. Pitch an AI-augmented production pipeline to cut asset costs from $400 to $3.
- Hiring Gaps (ATS): For job listings, offer an immediate outsourced solution to bypass the months-long time-to-hire.

**3. OUTPUT SCHEMA (JSON ONLY):**
{
    "brand_name": "The consumer brand or SaaS company name (proper case, no suffixes like Inc/LLC)",
    "niche": "Industry vertical (e.g. skincare, fashion, SaaS, supplements, fitness)",
    "company_size": "Best estimate: startup, small, medium, enterprise",
    "intent_signal": "Explicitly define the funding event or hiring trigger",
    "intent_tier": "hot (if funding < 48hrs) or warm (if hiring)",
    "confidence": "0.0 to 1.0",
    "decision_maker_name": "Extract the specific founder, CEO, or hiring manager name if mentioned. If none is found, return null.",
    "icebreaker_pitch": "Write a UNIQUE, highly specific 2-sentence pitch from a CREATOR to the BRAND. Synthesize their specific niche, their exact funding/hiring event, and offer your authentic UGC as the solution to their rising marginal CAC. DO NOT pitch an AI tool. DO NOT sound like a generic influencer. Speak like a business-savvy creator offering a highly profitable partnership."
}

**Critical Dynamic Rule:**
If the text is a founder complaining about ad costs (CAC/ROAS) on Hacker News or Twitter, your pitch MUST validate their specific frustration first. Example: 'I saw your thread on HN about your Meta ROAS tanking. Scaling spend always balloons marginal CAC. My unpolished UGC acts as an authenticity proxy to drive those acquisition costs back down without massive agency retainers.'

NEVER output anything except the JSON object.
"""

# Ollama API endpoint for chat completions
OLLAMA_CHAT_URL = f"{OLLAMA_BASE_URL}/api/chat"

# Maximum retries for LLM extraction (parse failures, timeouts)
MAX_RETRIES = 2


# ─────────────────────────────────────────────────────────
# JSON Sanitization — handles Qwen hallucination artifacts
# ─────────────────────────────────────────────────────────
def _sanitize_llm_json(raw: str) -> str:
    """
    Sanitize raw LLM output before JSON parsing.
    Handles common Qwen2.5 hallucination artifacts:
    - Markdown code block wrappers (```json ... ```)
    - Trailing commas before closing braces/brackets
    - Unescaped newlines inside string values
    - Leading/trailing text outside the JSON object
    """
    text = raw.strip()

    # Strip markdown code block wrappers
    if text.startswith("```"):
        lines = text.split("\n")
        start = 1 if lines[0].strip().startswith("```") else 0
        end = len(lines)
        for i in range(len(lines) - 1, -1, -1):
            if lines[i].strip() == "```":
                end = i
                break
        text = "\n".join(lines[start:end]).strip()

    # Remove trailing commas before } or ]
    # e.g., {"key": "value",} → {"key": "value"}
    text = re.sub(r',\s*([}\]])', r'\1', text)

    return text


def _extract_json_by_braces(text: str) -> str | None:
    """
    Extract the first complete JSON object from text using brace-depth
    counting. Handles nested objects, curly braces inside string values,
    and any surrounding text/markdown the LLM wraps around the JSON.

    Returns the raw JSON string or None if no balanced object is found.
    """
    start = text.find('{')
    if start == -1:
        return None

    depth = 0
    in_string = False
    escape_next = False

    for i in range(start, len(text)):
        char = text[i]

        if escape_next:
            escape_next = False
            continue

        if char == '\\' and in_string:
            escape_next = True
            continue

        if char == '"' and not escape_next:
            in_string = not in_string
            continue

        if in_string:
            continue

        if char == '{':
            depth += 1
        elif char == '}':
            depth -= 1
            if depth == 0:
                return text[start:i + 1]

    return None


# ─────────────────────────────────────────────────────────
# Phase 1: Deep Entity Extraction
# ─────────────────────────────────────────────────────────
def extract_entities(raw_text: str, source: str) -> dict | None:
    """
    Send raw scraped text to the local LLM for deep entity extraction.
    Returns the full entity graph (company, signal, locations, contacts,
    strategic_context) or None if extraction fails.

    Args:
        raw_text: The raw text from a scraper.
        source: The scraper source name for logging context.

    Returns:
        A dict matching the deep extraction schema, or None.
    """
    user_prompt = (
        f"Source: {source}\n"
        f"---\n"
        f"{raw_text[:2000]}\n"  # Cap at 2000 — entities cluster in first paragraphs
        f"---\n"
        f"Extract ALL entities and signals as JSON."
    )

    # Dynamic context window: short inputs get 2048, deep-scraped get 4096
    dynamic_ctx = 2048 if len(raw_text) < 1000 else OLLAMA_NUM_CTX

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            response = requests.post(
                OLLAMA_CHAT_URL,
                json={
                    "model": OLLAMA_MODEL,
                    "messages": [
                        {"role": "system", "content": DEEP_EXTRACTION_PROMPT},
                        {"role": "user", "content": user_prompt},
                    ],
                    "stream": False,
                    "options": {
                        "num_predict": 1024,
                        "num_ctx": 4096,
                        "temperature": OLLAMA_TEMPERATURE,
                    },
                },
                timeout=OLLAMA_TIMEOUT,
            )
            response.raise_for_status()
            data = response.json()

            content = data.get("message", {}).get("content", "").strip()

            if not content:
                logger.warning(
                    f"LLM: Empty response for source={source} "
                    f"(attempt {attempt}/{MAX_RETRIES})"
                )
                continue

            # Sanitize before parsing
            sanitized = _sanitize_llm_json(content)

            # Check for valid non-lead (empty JSON)
            if sanitized.strip() in ("{}", "{ }"):
                return None

            # Parse the JSON output
            entity = _parse_entity_json(sanitized)

            # Detect macro-news: LLM parsed OK but company is empty
            # (e.g., "Autonomous vehicles raised $21.4B" — no specific company)
            # Don't waste a retry on this — it's correct behavior, not a parse failure.
            if entity is None:
                # Check if it's a valid JSON with empty company (macro-news)
                # vs an actual parse failure (malformed JSON)
                try:
                    raw_parsed = json.loads(sanitized) if sanitized.strip().startswith("{") else None
                    if raw_parsed is None:
                        raw_json_str = _extract_json_by_braces(sanitized)
                        if raw_json_str:
                            raw_parsed = json.loads(re.sub(r',\s*([}\]])', r'\1', raw_json_str))
                except (json.JSONDecodeError, TypeError):
                    raw_parsed = None

                if raw_parsed and isinstance(raw_parsed, dict):
                    company_obj = raw_parsed.get("company", {})
                    if not company_obj or not company_obj.get("name"):
                        logger.debug(
                            f"LLM: Macro-news detected (no specific company) "
                            f"for source={source}. Skipping without retry."
                        )
                        return None  # Don't retry — this is correct LLM behavior

                logger.warning(
                    f"LLM: Failed to parse entity JSON on attempt "
                    f"{attempt}/{MAX_RETRIES} for source={source}. "
                    f"Raw: {content[:200]}"
                )
                continue

            # Inject source metadata
            entity["_source"] = source
            entity["_raw_text"] = raw_text[:2000]

            company_name = entity.get("company", {}).get("name", "Unknown")
            logger.debug(
                f"LLM: Deep-extracted '{company_name}' from {source} "
                f"(confidence: {entity.get('confidence')})"
            )
            return entity

        except requests.exceptions.Timeout:
            logger.warning(
                f"LLM: Timeout on attempt {attempt}/{MAX_RETRIES} "
                f"for source={source}"
            )
        except requests.exceptions.ConnectionError:
            logger.error(
                f"LLM: Cannot connect to Ollama at {OLLAMA_BASE_URL}. "
                f"Is the Ollama server running?"
            )
            return None
        except requests.exceptions.RequestException as e:
            logger.error(f"LLM: Request error for source={source}: {e}")
            return None

    logger.error(
        f"LLM: All {MAX_RETRIES} attempts failed for source={source}"
    )
    return None


def _parse_entity_json(text: str) -> dict | None:
    """
    Parse LLM output into a valid entity dict.
    We no longer perform strict validation here — just ensure it parses
    and has the 'company' root key. validator.py handles the rest.
    """
    # Try direct JSON parse first
    try:
        result = json.loads(text)
        if isinstance(result, dict) and "company" in result:
            return result
    except json.JSONDecodeError:
        pass

    # Fallback: extract first complete JSON object via brace-counting
    json_str = _extract_json_by_braces(text)
    if json_str:
        json_str = re.sub(r',\s*([}\]])', r'\1', json_str)
        try:
            result = json.loads(json_str)
            if isinstance(result, dict) and "company" in result:
                return result
        except json.JSONDecodeError:
            pass

    return None


# ─────────────────────────────────────────────────────────
# Legacy: Single-pass lead extraction (DEPRECATED)
# Kept for backward compatibility during migration.
# ─────────────────────────────────────────────────────────
def extract_lead(raw_text: str, source: str) -> dict | None:
    """
    DEPRECATED — Use extract_entities() for Phase 1.
    Send raw scraped text to the local LLM for structured lead extraction.

    Args:
        raw_text: The raw text from a scraper (job posting, press release, etc.)
        source: The scraper source name for logging context.

    Returns:
        A dict matching the lead schema, or None if extraction fails.
    """
    user_prompt = (
        f"Source: {source}\n"
        f"---\n"
        f"{raw_text[:2000]}\n"  # Cap input to avoid context overflow
        f"---\n"
        f"Extract the lead data as JSON."
    )

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            response = requests.post(
                OLLAMA_CHAT_URL,
                json={
                    "model": OLLAMA_MODEL,
                    "messages": [
                        {"role": "system", "content": SYSTEM_PROMPT},
                        {"role": "user", "content": user_prompt},
                    ],
                    "stream": False,
                    "options": {
                        "num_predict": 1024,
                        "num_ctx": 4096,
                        "temperature": OLLAMA_TEMPERATURE,
                    },
                },
                timeout=OLLAMA_TIMEOUT,
            )
            response.raise_for_status()
            data = response.json()

            # Extract the assistant's message content
            content = (
                data.get("message", {}).get("content", "").strip()
            )

            if not content:
                logger.warning(
                    f"LLM: Empty response for source={source} "
                    f"(attempt {attempt}/{MAX_RETRIES})"
                )
                continue

            # Silent skip for valid non-leads
            cleaned_content = content.replace("```json", "").replace("```", "").strip()
            if cleaned_content == "{}":
                return None

            # Parse the JSON output
            lead = _parse_llm_json(content)
            if lead is None:
                logger.warning(
                    f"LLM: Failed to parse JSON on attempt {attempt}/{MAX_RETRIES} "
                    f"for source={source}. Raw: {content[:200]}"
                )
                continue

            # Inject source metadata
            lead["source"] = source
            lead["raw_text"] = raw_text[:500]

            logger.debug(
                f"LLM: Extracted '{lead.get('brand_name')}' from {source} "
                f"(confidence: {lead.get('confidence')})"
            )
            return lead

        except requests.exceptions.Timeout:
            logger.warning(
                f"LLM: Timeout on attempt {attempt}/{MAX_RETRIES} "
                f"for source={source}"
            )
        except requests.exceptions.ConnectionError:
            logger.error(
                f"LLM: Cannot connect to Ollama at {OLLAMA_BASE_URL}. "
                f"Is the Ollama server running?"
            )
            return None
        except requests.exceptions.RequestException as e:
            logger.error(f"LLM: Request error for source={source}: {e}")
            return None

    logger.error(
        f"LLM: All {MAX_RETRIES} attempts failed for source={source}"
    )
    return None


def _parse_llm_json(text: str) -> dict | None:
    """
    DEPRECATED
    """
    # Sanitize first
    cleaned = _sanitize_llm_json(text)

    # Try direct JSON parse first
    try:
        result = json.loads(cleaned)
        if isinstance(result, dict):
            return result
    except json.JSONDecodeError:
        pass

    # Fallback: extract first complete JSON object via brace-counting
    json_str = _extract_json_by_braces(text)
    if json_str:
        json_str = re.sub(r',\s*([}\]])', r'\1', json_str)
        try:
            result = json.loads(json_str)
            if isinstance(result, dict):
                return result
        except json.JSONDecodeError:
            pass

    return None