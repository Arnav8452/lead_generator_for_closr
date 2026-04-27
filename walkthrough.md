# Closr: Complete System Walkthrough

A deep-dive into every component, decision, and data flow in the Closr autonomous B2B lead generation engine.

---

## Overview: The Data Flow

```
Raw Web Sources
    ↓
[Phase 1] Scrapers (7 parallel sources)
    ↓ raw_text + url + source
[Phase 2] LLM Extraction Worker (sequential, single-threaded)
    ├── Keyword Pre-filter   (O(1) string match — cheapest possible gate)
    ├── Air-Lock             (bart-large-mnli Zero-Shot, CPU)
    ├── Lexical Pulse        (sliding deque, O(N))
    ├── Sniper               (all-MiniLM-L6-v2 cosine, CPU)
    └── Qwen 2.5 7B (Ollama) → JSON entity {company, signal, contacts, locations}
    ↓
[Phase 2.5] Resolution Gauntlet (RAM dedup, before any API call)
    ├── Stage 1: Domain Grouping (pandas groupby)
    ├── Stage 2: String Locks   (Jaro-Winkler + Anti-Hierarchy Lock)
    └── Stage 3: Vector Scalpel (cosine similarity)
    ↓
[Phase 3] Enrichment Workers (5 concurrent threads)
    ├── Upsert company + signal → Supabase
    ├── Contact validation (The Bouncer + Normalizer)
    ├── Proximity Rank → Waterfall or ReAct decision
    ├── Waterfall (Rank 1–5): Prospeo LinkedIn → Hunter → Snov → Prospeo Named
    └── ReAct (Rank 97–99): Autonomous LLM Agent (up to 5 iterations)
    ↓
[Supabase] companies / proximal_contacts / company_signals
```

---

## Phase 1: Signal Sourcing (The Scrapers)

The pipeline has 7 distinct scrapers, each targeting a different source of **high-intent buying signals**. All 7 run **sequentially** and dump raw `ScrapedLead` objects into a shared list.

### `scrapers/google_news_funding.py`
Scrapes Google News RSS feeds for funding keywords ("Series A", "raises", "funding round"). Each article URL is deep-scraped by `polite_scraper.py`, which uses **readability-lxml** to strip nav bars, headers, and footers, returning clean semantic markdown for the LLM.

### `scrapers/ats_jobs.py`
Uses **JobSpy** to poll ATS job boards (Greenhouse, Lever, LinkedIn, Indeed) for open roles matching creator/influencer marketing titles. Configured to only pull postings from the last 48 hours (`JOBSPY_HOURS_OLD = 48`, up to 30 results).

### `scrapers/reddit_stealth.py`
Uses **curl-cffi** with real browser headers to bypass Reddit's anti-bot protection. Monitors 12+ subreddits (r/startups, r/marketing, r/Entrepreneur) for founders or brand managers explicitly asking for creator help.

### `scrapers/hacker_news.py`
Polls the HN Algolia API for "Ask HN: We're launching..." and "Show HN" posts targeting early-stage startup launches.

### `scrapers/remote_boards.py`
Scrapes niche remote job boards (Remotive, We Work Remotely) for marketing/creator-adjacent roles.

### `scrapers/meta_ads.py`
Calls the **Meta Marketing API** to search the public Ad Library. Flags any brand with `active_ads > META_ADS_SPIKE_THRESHOLD (10)` — they're spending heavily and likely need creator amplification.

### `scrapers/podcast_sponsors.py`
Fetches podcast episode descriptions and extracts brands mentioned as sponsors — a high-quality signal of proven creator budget.

---

## Phase 2: The Extraction Fortress

Every raw lead passes through a **4-stage defensive filter** before touching the GPU. Each gate is ordered cheapest → most expensive.

### Gate 0: Keyword Pre-filter
```python
SIGNAL_KEYWORDS = ["series a", "series b", "hiring", "influencer", "ugc", ...]
if not any(kw in raw_lower for kw in SIGNAL_KEYWORDS):
    return None  # Drop — zero cost
```
A simple `in` check. If none of the ~20 high-intent keywords are present, the lead is discarded with no model loaded.

### Gate 1: Air-Lock (`bart-large-mnli`, CPU)
- **Model:** `facebook/bart-large-mnli` (Zero-Shot Classification, forced `device=-1`)
- **Input:** Article headline or `brand_name_hint` (max 512 chars)
- **Labels:** 16 `TARGET_SIGNALS` (e.g., "funding round", "brand ambassador", "executive hire")
- **Threshold:** `AIRLOCK_CONFIDENCE_THRESHOLD = 0.65`
- **Result:** If the top confidence score is below 0.65, the lead is killed. The DOM is **never fetched** — saving bandwidth and compute.
- **Fail-open:** Model failure passes everything through.

### Gate 2: Lexical Pulse Check (O(N) sliding window)
```python
window: deque[str] = deque(maxlen=15)
for word in words:
    if clean in TRIGGER_VERBS:
        caps_count = sum(1 for w in window if _CAPS_PATTERN.match(w))
        if caps_count >= 2:
            return True  # PASS
```
A 15-word sliding `deque` over clean DOM text. Every time a `TRIGGER_VERB` ("raised", "hired", "launched", "partnered" — 50 total) appears, it counts nearby **capitalized proper nouns**. If `>= 2` are found near a trigger verb, it passes. Kills generic PR fluff ("The market is growing in Q4") that contains no named entities.

> Short texts (< 50 words) automatically pass — Reddit one-liners are not penalized.

### Gate 3: Sniper (`all-MiniLM-L6-v2`, CPU)
- **Model:** `all-MiniLM-L6-v2` (SentenceTransformers, CPU)
- **Intent Vector:** Pre-compiled centroid (normalized mean embedding) of all 16 `TARGET_SIGNALS`. Computed **once at startup**, cached.
- **Process:** DOM text chunked into 300-word / 50-word-overlap segments. Each chunk is encoded and dot-producted against the Intent Vector.
- **Threshold:** Chunks with cosine `< 0.25` are dropped. Only high-density chunks go to the LLM.
- **Fail-safe:** Always returns at least 1 chunk (the first one).

### LLM Extraction: Qwen 2.5 7B (Ollama)
Surviving chunks are joined and sent to `Qwen 2.5 7B` via Ollama's local HTTP API:
- `OLLAMA_FORMAT = "json"` — forces valid JSON output only
- `OLLAMA_TEMPERATURE = 0.0` — fully deterministic, zero hallucination variance
- `OLLAMA_NUM_CTX = 4096` — matched to 4GB VRAM budget
- `OLLAMA_TIMEOUT = 120s`

Expected output schema:
```json
{
  "company": { "name", "niche", "domain", "company_size" },
  "signal": { "type", "headline", "summary", "event_date" },
  "contacts": [{ "name", "title", "linkedin_url" }],
  "locations": [{ "city", "region", "country" }]
}
```

### The Killswitch: `is_lead_relevant()`
After LLM extraction, the pipeline checks niche/summary against the **Creator Economy Whitelist**:
- **Blacklist first:** `real estate`, `construction`, `mining`, `logistics`, `oil` → killed instantly.
- **Whitelist:** Must match a known vertical (skincare, SaaS, fitness) **or** a high-intent keyword (ugc, influencer, creator).
- **Hiring override:** If signal type is `"hiring"` AND a high-intent keyword is present, the killswitch is **bypassed** — a brand hiring an "Influencer Marketing Manager" is clearly in-scope, regardless of niche classification.

---

## Phase 2.5: The Resolution Gauntlet

Runs once on the entire batch, **in RAM, before any enrichment API call**. Merges duplicate company entries to prevent calling Hunter/Snov twice for "Acme Corp" vs "Acme Corp Inc".

### Stage 1: Domain Grouping
All entities with a resolved domain are grouped. Multiple articles about "Glossier" all resolve to `glossier.com` → merged into one entity.

### Stage 2: String Locks (Jaro-Winkler)
Company names are normalized (strips "Inc.", "LLC", "Corp", "Labs", "Technologies") and compared pairwise via **Jellyfish Jaro-Winkler**.
- **Threshold:** `GAUNTLET_JARO_THRESHOLD = 0.85`
- **Anti-Hierarchy Lock:** Contacts at different seniority tiers are **never merged**. A CEO and an Intern from the same company are distinct entities.

Seniority tiers:
```
Tier 0: intern, coordinator, associate, specialist
Tier 1: manager, lead, senior
Tier 2: director, head, vp
Tier 3: cmo, ceo, cto, founder, president
```

### Stage 3: Vector Scalpel
Remaining domain-less entities have their normalized names encoded with `all-MiniLM-L6-v2` and compared via cosine similarity.
- **Threshold:** `GAUNTLET_VECTOR_COSINE_THRESHOLD = 0.85`
- Catches semantic duplicates like "TikTok Shop" vs "TikTok Shopping" that Jaro-Winkler misses.

---

## Phase 3: Enrichment Workers

5 async threads (`ENRICHMENT_WORKERS = 5`) process deduplicated entities concurrently via `asyncio.to_thread()`.

### Step 1–2: Upsert & Domain Resolution
`upsert_company()` uses `on_conflict="name_normalized"` to safely merge duplicates. If no domain exists, `resolve_domain()` and `discover_domain()` attempt to find it via a Serper/Google search.

### Step 3: The Bouncer (`is_valid_contact()`)
Every contact must survive 4 checks:
1. **Structural:** Must contain a space (kills Reddit usernames).
2. **Character:** No digits, `@`, `_`, `!` (kills handles/email addresses).
3. **Semantic Name Check:** The name itself cannot contain department keywords ("Marketing Team", "Sales", "Founder"). Names that look like job titles are LLM hallucinations.
4. **Media Filter:** Kills journalists, writers, editors, bloggers — they don't control creator budgets.

### Step 4: Title Normalization + Proximity Ranking

**`normalize_title(raw_title, company_name)`:**
- Strips ASCII noise and `| @` separators
- Rejects title = exact company name (e.g., title="Gusto" when company="Gusto")
- Rejects junk single words: `"co"`, `"tldr"`, `"investor"`, `"ex"`
- Single-word titles must be in an exact whitelist (`"ceo"`, `"founder"`, `"vp"`, etc.)
- Returns `"Unknown"` on any failure

**`assign_proximity_rank(title)` routing table:**

| Rank | Constant | Meaning | Routed To |
|---|---|---|---|
| 1 | `PROXIMITY_CHECK_WRITER` | CMO, CEO, Founder, VP Marketing | Waterfall |
| 2 | `PROXIMITY_BUDGET` | Influencer Manager, Creator Partnerships | Waterfall |
| 3 | `PROXIMITY_PROXIMAL` | Social Media Manager, PR, Brand Manager | Waterfall |
| 5 | `PROXIMITY_LOW` | Generic Marketing, Sales, Director | Waterfall |
| 97 | `PROXIMITY_NO_MATCH` | Valid role but irrelevant (Supply Chain) | ReAct only |
| 98 | `PROXIMITY_BAD_TITLE` | No role keyword found at all | ReAct only |
| 99 | `PROXIMITY_UNRESOLVABLE` | Title is "Unknown" or missing | ReAct only |

### Step 5: LinkedIn Snippet Parser

LinkedIn Google Snippets arrive as:
```
"Alex Chen - Head of Creator Partnerships at Glossier | LinkedIn"
```

`extract_title_from_headline()` splits by separators (`-`, `|`, `•`) and scans each segment for role indicators ("ceo", "director", "manager", "vp", etc.). It strips the company name if it bled in. If no valid role found → title = `"Unknown"` → Rank 99.

### Step 6: The Enrichment Waterfall

Only contacts with `db_rank <= 5` enter the waterfall. Two modes:

**Named Search (preferred — name is known):**
1. Prospeo LinkedIn lookup (highest fidelity — uses LinkedIn URL directly)
2. Hunter Named Lookup (first + last + domain)
3. Snov Named Lookup (requires both first AND last name)
4. Prospeo Named Lookup (fallback)

**Title Search (name unknown):**
1. Hunter → Snov → Prospeo, by seniority title + domain

The first provider to return an email stops the cascade.

### Step 7: The Quota Manager

Keys are hashed (`MD5[:8]`) and tracked in `api_quotas.json`. Usage is persisted to disk after **every API call** to prevent cross-run amnesia.

- Google Search quotas include the current date in the hash → automatically reset at midnight, no cron job needed.
- Soft limits: Serper `2500`, Hunter `50`, Snov `50`, Prospeo `100`, Google `100/day`.
- Auto-rotation: When one key is exhausted, the next key from the list is tried silently.

---

## Phase 4: The ReAct Harness

For contacts with rank 97–99, the autonomous ReAct agent takes over. A Python-managed state machine prompts Qwen 2.5 7B in a loop.

### Concurrency Safety
`threading.Lock()` (`_ollama_lock`) serializes all Ollama HTTP calls from the 5 concurrent enrichment threads. Prevents VRAM contention on the RTX 3050.

### The Loop (max 5 iterations)
1. Build prompt: `goal`, `company`, `discovered_facts`, `failed_attempts`.
2. Call Ollama → parse `{synthesis, plan, action, action_input}`.
3. **Insanity Check:** Hash `(action, action_input)`. If already attempted, inject `[BLOCKED]` — forces the LLM to pivot strategies.
4. Execute tool → append observation to `discovered_facts`.
5. If `action == "conclude_research"` → exit with result.

### Available Tools

| Tool | Purpose |
|---|---|
| `osint_search` | General Serper/Google search |
| `fetch_linkedin_title` | Scrape LinkedIn snippet for real job title |
| `discover_email` | Cascades Prospeo/Hunter/Snov for a person |
| `validate_email_endpoint` | DNS MX + Prospeo email verification |
| `normalize_title` | Cleans a raw title string |
| `assign_proximity_rank` | Scores a title |
| `score_fit` | ICP scoring of a company (1–5) |
| `conclude_research` | Terminal action — returns result and exits |

### `fetch_linkedin_title` Sequence
When title = `"Unknown"`, the harness is designed to:
1. Call `fetch_linkedin_title(linkedin_url)` → get real title from snippet
2. Call `assign_proximity_rank(extracted_title)` → verify it's worth enriching
3. Only then call `discover_email()` — targeting the correct seniority

### Safe ReAct Routing
```python
if email and not (first_name or last_name):
    insert_unresolved_email(company_id, email, harness_lead)  # Parked safely

elif email and (first_name or last_name):
    upsert_proximal_contact(company_id, harness_contact)  # Linked to correct human
```

This eliminates the original "dangling `contact_id`" bug where the harness attached found emails to whichever contact was last in the loop.

---

## Supabase Schema

| Table | Key Columns | Conflict Key |
|---|---|---|
| `companies` | `name`, `name_normalized`, `domain`, `niche` | `name_normalized` |
| `company_signals` | `company_id`, `headline`, `summary`, `embedding` | insert only |
| `proximal_contacts` | `company_id`, `full_name`, `job_title`, `email`, `proximity_rank` | `company_id, full_name, job_title` |
| `company_locations` | `company_id`, `city`, `country` | `company_id, location_type, city, country` |

---

## Runtime Configuration Summary

| Parameter | Value | Location |
|---|---|---|
| LLM Model | `qwen2.5:7b` | `config.py` |
| LLM Temperature | `0.0` | `config.py` |
| Air-Lock Threshold | `0.65` | `config.py` |
| Sniper Cosine Threshold | `0.25` (softened from 0.35) | `extractor.py` |
| Jaro-Winkler Threshold | `0.85` | `config.py` |
| Vector Scalpel Threshold | `0.85` | `config.py` |
| Enrichment Waterfall Gate | `db_rank <= 5` | `main.py` |
| ReAct Max Iterations | `5` | `config.py` |
| Concurrent Enrichment Workers | `5` | `main.py` |
| JobSpy Hours Old | `48h` | `config.py` |
| Meta Ads Spike Threshold | `10 active ads` | `config.py` |
| Serper Quota Limit | `2,500/key` | `quota_manager.py` |
| Hunter Quota Limit | `50/key` | `quota_manager.py` |
| Prospeo Quota Limit | `100/key` | `quota_manager.py` |
| Google Search Quota | `100/key/day` | `quota_manager.py` |
