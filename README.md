# Closr — Autonomous B2B Lead Generation Engine

> Built for the **Creator Economy**. Runs entirely on local hardware. Zero cloud LLM costs.

Closr is a fully autonomous B2B lead generation pipeline that monitors the web for high-intent buying signals (funding rounds, hiring events, ad spend spikes), intelligently filters companies using a multi-stage AI fortress, extracts decision-makers with a local LLM, and enriches them with verified email addresses — all without touching a cloud AI API.

Optimized for a **4GB VRAM RTX 3050**. All AI inference runs locally via Ollama.

---

## 🗺️ Data Flow

```
Raw Web Sources
    ↓
[Phase 1] Scrapers (7 sources)
    ↓ raw_text + url + source
[Phase 2] Extraction Fortress (4 gates, cheapest → most expensive)
    ├── Keyword Pre-filter   (O(1) — zero model cost)
    ├── Air-Lock             (bart-large-mnli, CPU — Zero-Shot Classification)
    ├── Lexical Pulse        (sliding deque, O(N) — entity + trigger verb check)
    ├── Sniper               (all-MiniLM-L6-v2, CPU — cosine relevance pruning)
    └── Qwen 2.5 7B (Ollama) → JSON {company, signal, contacts, locations}
    ↓
[Phase 2.5] Resolution Gauntlet (RAM dedup — before any API call)
    ├── Domain Grouping      (pandas groupby on resolved domain)
    ├── String Locks         (Jellyfish Jaro-Winkler + Anti-Hierarchy Lock)
    └── Vector Scalpel       (all-MiniLM-L6-v2 cosine similarity)
    ↓
[Phase 3] Enrichment Workers (5 concurrent threads)
    ├── Upsert company + signal → Supabase
    ├── The Bouncer          (contact name/title validation)
    ├── Proximity Rank       (routes to Waterfall vs. ReAct)
    ├── Waterfall (Rank ≤5)  Prospeo LinkedIn → Hunter → Snov → Prospeo Named
    └── ReAct (Rank 97–99)   Autonomous Qwen agent, up to 5 iterations
    ↓
[Supabase] companies / proximal_contacts / company_signals
```

---

## 🛠️ Full Technology Stack

| Layer | Technology | Role |
|---|---|---|
| **Language** | Python 3.12 | Core runtime |
| **Async** | `asyncio` + `ThreadPoolExecutor` | 5 concurrent enrichment workers |
| **Local LLM** | Ollama + Qwen 2.5 7B | Entity extraction + ReAct agent |
| **Zero-Shot Filter** | `facebook/bart-large-mnli` (CPU) | Air-Lock gate — kills irrelevant leads |
| **Semantic Embeddings** | `all-MiniLM-L6-v2` (CPU) | Sniper gate + Gauntlet dedup |
| **Fuzzy Matching** | Jellyfish (Jaro-Winkler) | Company name deduplication |
| **Database** | Supabase (PostgreSQL + pgvector) | Persistent storage with on_conflict upserts |
| **Job Board Scraping** | JobSpy | ATS boards (Greenhouse, Lever, Indeed) |
| **Anti-Bot Scraping** | curl-cffi | Reddit, Remote Boards (bypasses Cloudflare) |
| **HTML Cleaning** | readability-lxml + BeautifulSoup4 | Strips nav bars, footers → clean markdown |
| **DNS Verification** | dnspython | Email MX record fallback verification |
| **Enrichment APIs** | Prospeo, Apollo, Snov.io, Hunter.io | Cascading email discovery waterfall |
| **Search APIs** | Serper.dev + Google Custom Search | LinkedIn discovery + OSINT for ReAct |

---

## 🏗️ Phase-by-Phase Architecture

### Phase 1 — Signal Sourcing (7 Scrapers)

| Scraper | Source | Signal |
|---|---|---|
| `google_news_funding.py` | Google News RSS | Series A/B/C funding announcements |
| `ats_jobs.py` | Greenhouse, Lever, Indeed | Creator/influencer marketing job postings (last 48h) |
| `reddit_stealth.py` | 12+ subreddits | Founders asking for creator/marketing help |
| `hacker_news.py` | HN Algolia API | "Ask HN" startup launches |
| `remote_boards.py` | Remotive, We Work Remotely | Marketing/creator remote roles |
| `meta_ads.py` | Meta Ad Library API | Brands with >10 active ads (high-spend signal) |
| `podcast_sponsors.py` | Podcast show notes | Brands actively paying for podcast sponsorships |

All scrapers use `readability-lxml` to strip garbage HTML into clean semantic markdown before feeding the LLM.

---

### Phase 2 — The Extraction Fortress (4 Gates)

**Gate 0 — Keyword Pre-filter (free)**
Simple `in` string check against 20 high-intent keywords ("series a", "ugc", "influencer", "hiring"). Discards leads with zero cost before any model loads.

**Gate 1 — Air-Lock** (`bart-large-mnli`, CPU, threshold: `0.65`)
Zero-shot classifies the article headline against 16 `TARGET_SIGNALS` ("funding round", "brand ambassador", "executive hire"). Below 0.65 confidence → the DOM is **never fetched**. Fails open if model is unavailable.

**Gate 2 — Lexical Pulse** (O(N) sliding deque)
Scans cleaned DOM text with a 15-word window. Requires **≥ 2 capitalized proper nouns** within 15 words of a TRIGGER_VERB (50 verbs: "raised", "hired", "launched", "partnered"). Kills generic PR fluff with no named entities. Short posts (< 50 words) pass automatically.

**Gate 3 — Sniper** (`all-MiniLM-L6-v2`, CPU, threshold: `0.25`)
Chunks DOM text into 300-word / 50-word-overlap segments. Encodes all chunks and compares them against a pre-compiled **Intent Vector** (centroid of all 16 TARGET_SIGNALS embeddings, compiled once at startup). Chunks scoring below 0.25 cosine are dropped. Always keeps at least 1 chunk.

**LLM — Qwen 2.5 7B** (Ollama, `temperature=0.0`, `format=json`)
Only surviving high-density chunks reach the GPU. Forced JSON mode eliminates markdown noise. Outputs structured entities: `company`, `signal`, `contacts[]`, `locations[]`.

**Killswitch — `is_lead_relevant()`**
Post-extraction, niche/summary is checked against the Creator Economy Whitelist:
- Blacklisted niches (real estate, construction, mining, oil) → killed instantly
- Must match a whitelist vertical (SaaS, DTC, fitness, beauty, media) **or** a high-intent keyword (ugc, influencer, creator)
- **Override:** Hiring signals with any creator keyword bypass the killswitch entirely

---

### Phase 2.5 — The Resolution Gauntlet (RAM Deduplication)

Runs on the entire extracted batch **before any enrichment API call**, preventing double-spending credits on the same company.

| Stage | Method | Threshold |
|---|---|---|
| Domain Grouping | pandas groupby on resolved domain | Exact match |
| String Locks | Jellyfish Jaro-Winkler | `0.85` |
| Anti-Hierarchy Lock | Seniority tier comparison | Cross-tier = never merge |
| Vector Scalpel | all-MiniLM-L6-v2 cosine | `0.85` |

**Anti-Hierarchy Lock seniority tiers:**
```
Tier 0: intern, coordinator, associate, specialist
Tier 1: manager, lead, senior
Tier 2: director, head, vp
Tier 3: cmo, ceo, cto, founder, president
```
Contacts from different tiers are **always** kept as separate entities, even if the company name matches perfectly.

---

### Phase 3 — Enrichment Workers (5 Concurrent Threads)

**The Bouncer** — `is_valid_contact(name, title)` validates every extracted contact:
1. **Structural:** Name must contain a space (kills Reddit usernames like `UpperLifeguard8284`)
2. **Character:** No digits, `@`, `_`, `!` (kills handles and email addresses)
3. **Semantic:** Name cannot contain department keywords — "Marketing Team", "Founder", "Sales" are hallucinations
4. **Media Filter:** Kills journalists, writers, editors — they don't hold creator budgets

**Title Normalization** — `normalize_title(raw_title, company_name)`:
- Strips ASCII noise and `| @` separators
- Rejects title = exact company name (e.g., title="Gusto" at company="Gusto")
- Rejects known junk single words: `"co"`, `"tldr"`, `"investor"`, `"ex"`
- Single-word titles must be in a strict whitelist (`"ceo"`, `"founder"`, `"vp"`, etc.)
- Returns `"Unknown"` on failure

**LinkedIn Snippet Parsing** — `extract_title_from_headline()`:
Google Snippets often return raw LinkedIn headlines like `"Alex Chen - Head of Creator Partnerships at Glossier | LinkedIn"`. The parser splits on separators (`-`, `|`, `•`), scans each segment for role indicators, strips bleeding company names, and returns the clean title. Falls back to `"Unknown"` if no role is found.

---

### 🎯 Proximity Routing

Every contact is scored and routed based on seniority relevance:

| Rank | Constant | Who | Action |
|---|---|---|---|
| 1 | `PROXIMITY_CHECK_WRITER` | CMO, CEO, Founder, VP Marketing | → API Waterfall |
| 2 | `PROXIMITY_BUDGET` | Influencer Manager, Creator Partnerships | → API Waterfall |
| 3 | `PROXIMITY_PROXIMAL` | Social Media Manager, PR, Brand Manager | → API Waterfall |
| 5 | `PROXIMITY_LOW` | Generic Marketing, Sales, Director | → API Waterfall |
| 97 | `PROXIMITY_NO_MATCH` | Valid role, wrong department (Supply Chain) | → ReAct Agent only |
| 98 | `PROXIMITY_BAD_TITLE` | No recognizable role keyword | → ReAct Agent only |
| 99 | `PROXIMITY_UNRESOLVABLE` | Title is "Unknown" or blank | → ReAct Agent only |

**Waterfall gate:** `db_rank <= 5`. Contacts ranked 97–99 never touch the paid enrichment APIs.

---

### 📬 The Enrichment Waterfall

**Named Search** (when contact name is known — preferred):
1. Prospeo LinkedIn lookup (direct LinkedIn URL → highest accuracy)
2. Apollo LinkedIn lookup (fallback URL search)
3. Hunter Named Lookup (first + last + domain)
4. Snov Named Lookup (requires both first AND last name)
5. Apollo Named Lookup (accepts partial last names)
6. Prospeo Named Lookup (final fallback)

**Title Search** (when contact name is unknown):
1. Hunter → Snov → Prospeo, searching by seniority title + domain

First provider to return an email stops the cascade.

---

### 💳 The Quota Manager

Keys are never stored in plain text. They are hashed (`MD5[:8]`) and tracked in `api_quotas.json`. Usage is written to disk after **every API call**.

- **Auto-rotation:** When one key hits its limit, the next key in the list is silently used.
- **Google reset:** Google Search quotas embed the current date in the tracking hash — automatically fresh each day, no cron required.

| API | Limit per Key |
|---|---|
| Serper.dev | 2,500 requests |
| Apollo.io | 75 requests |
| Hunter.io | 50 requests |
| Snov.io | 50 requests |
| Prospeo | 100 requests |
| Google Custom Search | 100 requests/day |

---

### Phase 4 — The ReAct Harness (Autonomous Agent)

For contacts with proximity rank 97–99, an autonomous ReAct state-machine takes over. It loops Qwen 2.5 7B up to 5 times with a full tool kit to find and verify contact information.

**Concurrency:** `threading.Lock()` serializes all Ollama calls across the 5 enrichment threads — prevents VRAM contention on the RTX 3050.

**The Loop:**
1. Build a prompt with `goal`, `company`, `discovered_facts`, `failed_attempts`
2. Parse Qwen output: `{synthesis, plan, action, action_input}`
3. **Insanity Check:** Hash `(action, action_input)` — if repeated, inject `[BLOCKED]` and force a new strategy
4. Execute the tool, append the observation
5. `conclude_research` action exits the loop

**Available Tools:**

| Tool | What it does |
|---|---|
| `osint_search` | General Serper/Google web search |
| `fetch_linkedin_title` | Scrape a LinkedIn URL snippet for the real job title |
| `discover_email` | Cascade Prospeo/Hunter/Snov for a named person |
| `validate_email_endpoint` | DNS MX + Prospeo live email verification |
| `normalize_title` | Clean a raw title string |
| `assign_proximity_rank` | Score a title (1–99) |
| `score_fit` | ICP score a company (1–5) |
| `conclude_research` | Terminal action — return `{status, lead_data}` |

**`fetch_linkedin_title` sequence** (for Unknown titles):
1. `fetch_linkedin_title(linkedin_url)` → get real title from Google snippet
2. `assign_proximity_rank(title)` → confirm it's worth pursuing
3. `discover_email()` → only then spend enrichment credits

**Safe Routing** (eliminates the dangling `contact_id` bug):
```python
if email and not (first_name or last_name):
    # Name unknown — park safely, never corrupt other contacts
    insert_unresolved_email(company_id, email, harness_lead)

elif email and (first_name or last_name):
    # Upsert safely — creates or merges to the correct human
    upsert_proximal_contact(company_id, harness_contact)
```

---

## 🗄️ Supabase Schema

| Table | Conflict Key | Key Fields |
|---|---|---|
| `companies` | `name_normalized` | name, domain, niche, company_size |
| `company_signals` | insert only | company_id, headline, summary, embedding |
| `proximal_contacts` | `company_id, full_name, job_title` | email, proximity_rank, email_source, linkedin_url |
| `company_locations` | `company_id, location_type, city, country` | city, region, country |

---

## ⚙️ Runtime Configuration

| Parameter | Value | File |
|---|---|---|
| LLM Model | `qwen2.5:7b` | `config.py` |
| LLM Temperature | `0.0` | `config.py` |
| LLM Context Window | `4096` tokens | `config.py` |
| Air-Lock Threshold | `0.65` | `config.py` |
| Sniper Cosine Threshold | `0.25` | `extractor.py` |
| Jaro-Winkler Threshold | `0.85` | `config.py` |
| Vector Scalpel Threshold | `0.85` | `config.py` |
| Waterfall Gate | `proximity_rank <= 5` | `main.py` |
| ReAct Max Iterations | `5` | `config.py` |
| Enrichment Workers | `5` concurrent | `main.py` |
| JobSpy Lookback | `48 hours` | `config.py` |
| Meta Ads Spike Threshold | `10 active ads` | `config.py` |

---

Run stats (single pipeline execution):
  Sources scraped:    241 signals
  Passed extraction:  56  (23% pass-through rate)
  Companies:          42
  Contacts mapped:    106 (avg 2.5 / company)
  Emails verified:    27
  LinkedIn profiles:  97
  Runtime:            ~92 minutes

## 📦 Setup

**1. Requirements**
- Python 3.12+
- NVIDIA GPU with ≥4GB VRAM
- [Ollama](https://ollama.com/) running locally

**2. Pull the model**
```bash
ollama pull qwen2.5:7b
```

**3. Install dependencies**
```bash
pip install -r requirements.txt
```

**4. Configure environment**
```bash
cp .env.example .env
# Fill in your keys
```
```env
SUPABASE_URL=your_supabase_url
SUPABASE_KEY=your_supabase_service_role_key

# Comma-separated for auto-rotation
HUNTER_API_KEYS=key1,key2
SNOV_CREDENTIALS=client_id1:secret1,client_id2:secret2
PROSPEO_API_KEYS=key1,key2
SERPER_API_KEYS=key1,key2
GOOGLE_SEARCH_API_KEYS=key1
GOOGLE_SEARCH_CXS=cx1

# Optional
META_ACCESS_TOKEN=your_meta_token
OLLAMA_MODEL=qwen2.5:7b
LOG_LEVEL=INFO
```

**5. Run**
```bash
python main.py
```

**Database cleanup utility** (after a bad run — preserves enriched contacts):
```bash
python clean_db.py
```

---

## 📄 License
MIT License.
