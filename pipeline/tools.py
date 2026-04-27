"""
Closr — V2 ReAct Tool Registry

All tools callable by the DeepResearchHarness (pipeline/harness.py).
Each function returns a JSON-serializable value.
The dispatcher execute_tool() catches all exceptions so harness loop never crashes.

Tools:
    score_fit(company_info)           -> int  (1-5 ICP score)
    normalize_title(raw_text)         -> str
    assign_proximity_rank(title)      -> int  (1=best, 99=unknown)
    osint_search(query)               -> list[dict]  (Serper results)
    validate_email_endpoint(email)    -> bool
    conclude_research(status, lead_data) -> dict  (terminal harness action)
"""

import logging
import re

from config import (
    CREATOR_ECONOMY_WHITELIST,
    ENTERPRISE_BLOCKLIST,
)
from utils.quota_manager import quota_manager

logger = logging.getLogger("closr.pipeline.tools")

PROXIMITY_CHECK_WRITER = 1
PROXIMITY_BUDGET = 2
PROXIMITY_PROXIMAL = 3
PROXIMITY_LOW = 5
PROXIMITY_NO_MATCH = 97
PROXIMITY_BAD_TITLE = 98
PROXIMITY_UNRESOLVABLE = 99


# ─────────────────────────────────────────────────────────
# Funding & Hiring signal keywords for score_fit
# ─────────────────────────────────────────────────────────
_FUNDING_SIGNALS: set[str] = {
    "funding", "series a", "series b", "series c", "seed", "raised",
    "raise", "investment", "funded", "backed", "secured", "round",
}
_HIRING_SIGNALS: set[str] = {
    "hiring", "ugc", "influencer", "creator", "ambassador",
    "partnership", "looking for", "seeking",
}


# ─────────────────────────────────────────────────────────
# Tool: score_fit
# ─────────────────────────────────────────────────────────
def score_fit(company_info: dict) -> int:
    """
    Score a company's ICP fit on a 1-5 scale.

    5 — Deep niche match + funding/hiring signal (perfect target)
    4 — Niche match AND intent keyword present
    3 — Niche match OR intent keyword present (baseline for enrichment)
    2 — Adjacent vertical (health, food, lifestyle) but no clear signal
    1 — Hard reject: enterprise blocklist or blacklisted vertical

    Args:
        company_info: dict with keys: name, niche, signal_summary, signal_type

    Returns:
        int 1-5
    """
    name = (company_info.get("name") or "").lower().strip()
    niche = (company_info.get("niche") or "").lower()
    signal_summary = (company_info.get("signal_summary") or "").lower()
    signal_type = (company_info.get("signal_type") or "").lower()
    text = f"{niche} {signal_summary}"

    # ── Hard reject ──────────────────────────────────────
    normalized_name = re.sub(r'[^a-z0-9]', '', name)
    for blocked in ENTERPRISE_BLOCKLIST:
        if blocked in normalized_name:
            return 1

    for blacklisted in CREATOR_ECONOMY_WHITELIST.get("blacklist", []):
        if blacklisted in text:
            return 1

    # ── Relevance signals ────────────────────────────────
    verticals = CREATOR_ECONOMY_WHITELIST.get("verticals", [])
    intent_kws = CREATOR_ECONOMY_WHITELIST.get("intent_keywords", [])

    in_vertical = any(v in text for v in verticals)
    high_intent = any(i in text for i in intent_kws)

    has_funding = signal_type in ("funding",) or any(s in text for s in _FUNDING_SIGNALS)
    has_hiring = signal_type in ("hiring",) or any(s in text for s in _HIRING_SIGNALS)
    has_signal = has_funding or has_hiring

    # ── Scoring ──────────────────────────────────────────
    if (in_vertical or high_intent) and has_signal:
        return 5
    if in_vertical and high_intent:
        return 4
    if in_vertical or high_intent:
        return 3
    # Hiring signal alone is a baseline pass — ATS jobs always have buying intent
    # even if the niche wasn't cleanly classified by the LLM
    if has_hiring:
        return 3
    # Adjacent verticals — useful but not ideal
    adjacent = {"health", "food", "lifestyle", "consumer", "apparel", "fitness"}
    if any(adj in text for adj in adjacent):
        return 2
    return 1


# ─────────────────────────────────────────────────────────
# Tool: normalize_title
# ─────────────────────────────────────────────────────────
_JUNK_TITLE_PATTERNS: list[str] = [
    r"(?i)we'?re hiring.*$",
    r"(?i)\(?hiring\)?.*$",
    r"(?i)\|?\s*forbes.*$",
    r"(?i)\|?\s*top voice.*$",
    r"(?i)\|?\s*ex-[\w\s]+.*$",
    r"(?i)\s*[-|]?\s*linkedin\s*$",
    r"(?i)\s*[-|]?\s*professional profile\s*$",
    r"\.\.\.$",
    r"(?i)\(?(he|she|they)/(him|her|them)\)?",
]
_LOCATION_WORDS: set[str] = {
    "area", "united states", "uk", "canada", "london", "california",
    "new york", "san francisco", "greater", "region", "remote",
}
_ROLE_WORDS: set[str] = {
    "manager", "director", "vp", "head", "founder", "ceo", "cmo",
    "marketing", "sales", "creator", "partner", "lead", "officer", "specialist",
}


def normalize_title(raw_text: str, company_name: str = "") -> str:
    """
    Clean and normalize a job title string.
    Returns "Unknown" for junk/empty/non-title inputs.
    """
    if not raw_text:
        return "Unknown"
    t = raw_text.strip()
    if t.lower() in ("null", "none", "unknown", "na", "n/a", "undefined", ""):
        return "Unknown"

    t = t.encode("ascii", "ignore").decode("ascii")
    for pattern in _JUNK_TITLE_PATTERNS:
        t = re.sub(pattern, "", t)

    # Split on pipe/at separators, keep first segment
    t = re.split(r"\s+[\|@]\s+", t)[0]

    t_lower = t.lower()
    
    # 1. Filter out exact company name matches
    if company_name and t_lower == company_name.lower():
        return "Unknown"
        
    # 2. Filter out known junk single words
    junk_single_words = {"co", "tldr", "investor", "ex", "the", "a", "an", "team", "company", "inc", "llc", "ltd"}
    if t_lower in junk_single_words:
        return "Unknown"

    # 3. Single word strict filtering
    words = t_lower.split()
    if len(words) == 1:
        valid_single_words = {"ceo", "cmo", "cro", "cto", "cfo", "coo", "vp", "founder", "president", "owner", "director", "manager", "lead", "specialist"}
        if words[0] not in valid_single_words:
            return "Unknown"

    # If the string looks like a location without any role word — it's not a title
    if any(loc in t_lower for loc in _LOCATION_WORDS) and not any(r in t_lower for r in _ROLE_WORDS):
        return "Unknown"

    t = " ".join(t.split()).title()
    return t if len(t) >= 2 else "Unknown"


# ─────────────────────────────────────────────────────────
# Tool: assign_proximity_rank
# ─────────────────────────────────────────────────────────
def assign_proximity_rank(title: str) -> int:
    """
    Ranks a contact based on their proximity to the creator sponsorship budget.
    """
    if not title or title.lower() in ("unknown", "n/a", "none"):
        return PROXIMITY_UNRESOLVABLE

    t = title.lower()

    # RANK 1: THE CHECK-WRITERS (Absolute highest priority)
    rank_1_keywords = [
        "cmo", "chief marketing", "founder", "co-founder", "ceo", "president",
        "vp of marketing", "vp marketing", "head of marketing", "head of growth",
        "vp of growth", "chief revenue officer", "cro", "owner"
    ]
    if any(kw in t for kw in rank_1_keywords):
        return PROXIMITY_CHECK_WRITER

    # RANK 2: THE BUDGET HANDLERS (Direct targets)
    rank_2_keywords = [
        "influencer", "creator", "ugc", "affiliate", "kol", 
        "partnership", "talent", "brand ambassador"
    ]
    if any(kw in t for kw in rank_2_keywords):
        return PROXIMITY_BUDGET

    # RANK 3: THE PROXIMAL OPERATORS (Can forward the pitch to the right person)
    rank_3_keywords = [
        "social media", "community manager", "public relations", "pr", 
        "communications", "brand manager", "marketing manager", "growth manager"
    ]
    if any(kw in t for kw in rank_3_keywords):
        return PROXIMITY_PROXIMAL

    # RANK 5: Weak match (valid role, but maybe adjacent marketing or generic manager/director)
    rank_5_keywords = [
        "marketing", "sales", "growth", "strategy", "director", "manager", "lead", "specialist"
    ]
    if any(kw in t for kw in rank_5_keywords):
        return PROXIMITY_LOW

    # Check if it has any role word at all
    has_role = any(r in t for r in _ROLE_WORDS)
    if not has_role:
        return PROXIMITY_BAD_TITLE

    # Valid role word, but not in our targeted lists
    return PROXIMITY_NO_MATCH


# ─────────────────────────────────────────────────────────
# Tool: osint_search
# ─────────────────────────────────────────────────────────
def osint_search(query: str) -> list[dict]:
    """
    Execute a general web search via Serper (Google Custom Search is restricted to LinkedIn).
    
    Returns top 5 organic results as list of {title, link, snippet} dicts.
    Returns [{"error": "..."}] if all keys are exhausted or search fails.
    """
    try:
        from enrichment.contact_discovery import _execute_search
        results = _execute_search(query, num=5, restrict_to_linkedin=False)
        if not results:
            return [{"error": "All OSINT Search keys exhausted or no results found."}]
        return results
    except Exception as e:
        logger.error(f"osint_search error for query '{query[:60]}': {e}")
        return [{"error": str(e)}]


# ─────────────────────────────────────────────────────────
# Tool: fetch_linkedin_title
# ─────────────────────────────────────────────────────────
def fetch_linkedin_title(linkedin_url: str, company_name: str = "") -> dict:
    """
    Fetch the actual job title of a person using their LinkedIn URL.
    This runs a targeted search and aggressively parses the snippet headline.
    """
    try:
        from enrichment.contact_discovery import _execute_search, extract_title_from_headline
        # We search specifically for their exact URL to get the Google index snippet
        query = f'site:linkedin.com/in "{linkedin_url}"'
        results = _execute_search(query, num=1, restrict_to_linkedin=True)
        
        if not results:
            return {"error": "No search results found for this LinkedIn URL."}
            
        headline = results[0].get("title", "")
        extracted_title = extract_title_from_headline(headline, company_name)
        
        if extracted_title:
            return {"title": extracted_title}
        return {"error": "Could not parse a valid title from the headline snippet."}
    except Exception as e:
        logger.error(f"fetch_linkedin_title error for '{linkedin_url}': {e}")
        return {"error": str(e)}


# ─────────────────────────────────────────────────────────
# Tool: validate_email_endpoint
# ─────────────────────────────────────────────────────────
def validate_email_endpoint(email: str) -> bool:
    """
    Verify an email using Prospeo's verifier + DNS MX fallback.
    Delegates to enrichment/validator.py.
    """
    try:
        from enrichment.validator import verify_email
        return verify_email(email)
    except Exception as e:
        logger.warning(f"validate_email_endpoint error: {e}")
        return False


# ─────────────────────────────────────────────────────────
# Tool: discover_email
# ─────────────────────────────────────────────────────────
def discover_email(first_name: str = "", last_name: str = "", domain: str = "", company_name: str = "", linkedin_url: str = "") -> dict:
    """
    Search databases for a specific person's email using their name or LinkedIn URL.
    """
    import urllib.parse
    
    # 1. Sanitize inputs to prevent API 400 Bad Requests
    first_name = first_name.strip() if first_name else ""
    last_name = last_name.strip() if last_name else ""
    linkedin_url = linkedin_url.strip() if linkedin_url else ""
    company_name = company_name.strip() if company_name else ""
    
    # Clean out LLM hallucinated null values
    if first_name.lower() in ("unknown", "none", "n/a", "null", "undefined"):
        first_name = ""
    if last_name.lower() in ("unknown", "none", "n/a", "null", "undefined"):
        last_name = ""
    
    if domain:
        domain = domain.strip().lower()
        if domain.startswith("http"):
            domain = urllib.parse.urlparse(domain).netloc
        domain = domain.replace("www.", "").split('/')[0]

    try:
        from enrichment.hunter import hunter_named_lookup
        from enrichment.snov import snov_named_lookup
        from enrichment.prospeo import prospeo_linkedin_lookup
        from enrichment.apollo import apollo_linkedin_lookup, apollo_named_lookup as apollo_named
        
        # 1. Prospeo via LinkedIn
        if linkedin_url:
            key = quota_manager.get_prospeo_key()
            if key:
                result = prospeo_linkedin_lookup(linkedin_url, key)
                if result and result.get("email"):
                    return {"email": result["email"], "source": "prospeo_linkedin", "verified": result.get("verified")}

        # 2. Apollo via LinkedIn (great fallback — large database)
        if linkedin_url:
            key = quota_manager.get_apollo_key()
            if key:
                result = apollo_linkedin_lookup(linkedin_url, key)
                if result and result.get("email"):
                    return {"email": result["email"], "source": "apollo_linkedin", "verified": result.get("verified")}

        # 3. Snov via Name + Domain (requires last_name or throws 400)
        if first_name and last_name and domain:
            creds = quota_manager.get_snov_credentials()
            if creds:
                client_id, client_secret = creds
                result = snov_named_lookup(domain, first_name, last_name, client_id, client_secret)
                if result and result.get("email"):
                    return {"email": result["email"], "source": "snov", "verified": result.get("verified")}

        # 4. Hunter via Domain + Name
        if first_name and domain:
            key = quota_manager.get_hunter_key()
            if key:
                result = hunter_named_lookup(domain, first_name, last_name, key, company=company_name)
                if result and result.get("email"):
                    return {"email": result["email"], "source": "hunter", "verified": result.get("verified")}

        # 5. Apollo named lookup (accepts partial last name — wider net)
        if first_name and domain:
            key = quota_manager.get_apollo_key()
            if key:
                result = apollo_named(first_name, last_name, domain, key, company=company_name)
                if result and result.get("email"):
                    return {"email": result["email"], "source": "apollo_named", "verified": result.get("verified")}

        return {"error": "No email found in databases for the provided inputs."}

    except Exception as e:
        logger.error(f"discover_email error: {e}")
        return {"error": str(e)}


# ─────────────────────────────────────────────────────────
# Tool: conclude_research (terminal action)
# ─────────────────────────────────────────────────────────
def conclude_research(status: str, lead_data: dict | None = None) -> dict:
    """
    Terminal action that signals the harness to break the ReAct loop.
    Args:
        status:    "success" or "failed"
        lead_data: Structured lead result (name, email, linkedin, etc.)
    """
    return {
        "status": status,
        "lead_data": lead_data or {},
    }


# ─────────────────────────────────────────────────────────
# Tool Dispatcher
# ─────────────────────────────────────────────────────────
TOOL_REGISTRY: dict[str, callable] = {
    "score_fit": score_fit,
    "normalize_title": normalize_title,
    "assign_proximity_rank": assign_proximity_rank,
    "osint_search": osint_search,
    "fetch_linkedin_title": fetch_linkedin_title,
    "validate_email_endpoint": validate_email_endpoint,
    "discover_email": discover_email,
    "conclude_research": conclude_research,
}


def execute_tool(name: str, args: dict) -> dict:
    """
    Safely dispatch a tool call by name.
    All exceptions are caught and returned as {"error": "..."} so the
    harness can inject an Observation without crashing the loop.
    """
    fn = TOOL_REGISTRY.get(name)
    if not fn:
        available = list(TOOL_REGISTRY.keys())
        return {"error": f"Unknown tool '{name}'. Available: {available}"}
    try:
        result = fn(**args)
        return {"result": result}
    except TypeError as e:
        return {"error": f"Bad arguments for tool '{name}': {e}"}
    except Exception as e:
        logger.error(f"Tool '{name}' raised unexpectedly: {e}")
        return {"error": str(e)}
