"""
Closr — LLM Extraction Pipeline (Ollama / Local)
Sends scraped raw text to a local LLM for structured brand + intent extraction.
Output is strict JSON conforming to the lead schema.

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
# System Prompt — engineered for reliable JSON extraction
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


def extract_lead(raw_text: str, source: str) -> dict | None:
    """
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
                        "num_ctx": OLLAMA_NUM_CTX,
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
    Parse LLM output into a valid lead dict. Handles common LLM quirks:
    - Markdown code blocks (```json ... ```)
    - Leading/trailing whitespace or text
    - Nested JSON objects
    - Missing or extra fields
    """
    # Strip markdown code block wrappers if present
    cleaned = text.strip()
    if cleaned.startswith("```"):
        # Remove opening ```json or ``` line
        lines = cleaned.split("\n")
        # Find the first and last ``` lines
        start = 0
        end = len(lines)
        for i, line in enumerate(lines):
            if line.strip().startswith("```") and i == 0:
                start = i + 1
            elif line.strip() == "```":
                end = i
                break
        cleaned = "\n".join(lines[start:end]).strip()

    # Try direct JSON parse first
    try:
        result = json.loads(cleaned)
        if isinstance(result, dict):
            return _validate_lead_schema(result)
    except json.JSONDecodeError:
        pass

    # Fallback: extract first complete JSON object via brace-counting
    json_str = _extract_json_by_braces(text)
    if json_str:
        try:
            result = json.loads(json_str)
            if isinstance(result, dict):
                return _validate_lead_schema(result)
        except json.JSONDecodeError:
            pass

    return None


def _validate_lead_schema(data: dict) -> dict | None:
    """
    Ensure the LLM output has the required fields with correct types.
    Returns a cleaned dict or None if critically malformed.
    """
    required = ["brand_name", "confidence"]

    for field in required:
        if field not in data:
            return None

    # Coerce confidence to float
    try:
        data["confidence"] = float(data["confidence"])
    except (ValueError, TypeError):
        data["confidence"] = 0.0

    # Ensure all expected string fields exist (with defaults)
    defaults = {
        "niche": None,
        "company_size": None,
        "intent_signal": None,
        "intent_tier": "cold",
        "decision_maker_name": None,
        "icebreaker_pitch": None,
    }
    for field, default in defaults.items():
        if field not in data:
            data[field] = default

    return data
