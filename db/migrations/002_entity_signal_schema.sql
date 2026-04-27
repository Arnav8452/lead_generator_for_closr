-- ═══════════════════════════════════════════════════════════
-- Closr — Phase 1: Entity & Signal Schema (DESTRUCTIVE)
-- Migration 002 — Run in Supabase SQL Editor.
-- ⚠️  Coordinate with Lakshay before executing.
-- ═══════════════════════════════════════════════════════════

-- ── Step 0: Enable pgvector ────────────────────────────────
CREATE EXTENSION IF NOT EXISTS vector;

-- ── Step 0.5: Drop legacy lead tables ──────────────────────
-- Legacy data is shallow / brittle and will pollute the
-- vector space. pipeline_runs and enrichment_usage are KEPT.
DROP TABLE IF EXISTS daily_pool CASCADE;
DROP TABLE IF EXISTS unenriched_leads CASCADE;


-- ── Step 1: Companies (Hub Entity) ─────────────────────────
CREATE TABLE IF NOT EXISTS companies (
    id              BIGSERIAL PRIMARY KEY,
    name            TEXT NOT NULL,
    name_normalized TEXT NOT NULL,  -- lowercase, suffix-stripped for dedup
    domain          TEXT,           -- resolved via Clearbit
    niche           TEXT,
    company_size    TEXT,           -- startup | small | medium | enterprise
    logo_url        TEXT,           -- from Clearbit autocomplete
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    updated_at      TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (name_normalized)
);
CREATE INDEX IF NOT EXISTS idx_companies_domain ON companies (domain);


-- ── Step 2: Company Locations (1:N) ────────────────────────
CREATE TABLE IF NOT EXISTS company_locations (
    id              BIGSERIAL PRIMARY KEY,
    company_id      BIGINT NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    location_type   TEXT NOT NULL DEFAULT 'unknown',  -- hq | hiring | office | expansion
    city            TEXT,
    region          TEXT,        -- state / province
    country         TEXT,
    raw_string      TEXT,        -- original extracted string before normalization
    source_url      TEXT,        -- which article/posting this came from
    extracted_at    TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (company_id, location_type, city, country)
);
CREATE INDEX IF NOT EXISTS idx_locations_company ON company_locations (company_id);


-- ── Step 3: Company Signals (1:N, with Vector Embeddings) ──
CREATE TABLE IF NOT EXISTS company_signals (
    id              BIGSERIAL PRIMARY KEY,
    company_id      BIGINT NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    signal_type     TEXT NOT NULL,   -- funding | hiring | expansion | product_launch | distress | ad_spend
    headline        TEXT,            -- e.g. "Series A $12M led by Sequoia"
    summary         TEXT,            -- 2-3 sentence context from LLM
    source_url      TEXT,
    source_name     TEXT,            -- google_news, ats_jobs, reddit_stealth, etc.
    event_date      TIMESTAMPTZ,     -- when the event happened (LLM-extracted)
    raw_text        TEXT,            -- original scraped text (capped at 3000 chars)
    embedding       vector(768),     -- nomic-embed-text via Ollama
    created_at      TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_signals_company ON company_signals (company_id);
CREATE INDEX IF NOT EXISTS idx_signals_type    ON company_signals (signal_type);

-- Vector index strategy:
--   Phase 1 (< 1000 rows): exact scan (no index needed)
--   Phase 2 (1000+ rows) : uncomment the IVFFlat index below
--   Phase 3 (paid tier)   : switch to HNSW for sub-ms latency
--
-- CREATE INDEX idx_signals_embedding ON company_signals
--     USING ivfflat (embedding vector_cosine_ops) WITH (lists = 20);


-- ── Step 4: Proximal Contacts (1:N) ────────────────────────
CREATE TABLE IF NOT EXISTS proximal_contacts (
    id              BIGSERIAL PRIMARY KEY,
    company_id      BIGINT NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    full_name       TEXT NOT NULL,
    job_title       TEXT,
    linkedin_url    TEXT,
    email           TEXT,            -- nullable — may never be found
    email_verified  BOOLEAN DEFAULT FALSE,
    email_source    TEXT,            -- hunter | snov | regex | crawl | null
    proximity_rank  INT DEFAULT 99,  -- 1 = primary target, 2 = direct report, 3 = dept peer, 99 = unknown
    source_url      TEXT,            -- where we found this person
    source_name     TEXT,
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    updated_at      TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (company_id, full_name, job_title)
);
CREATE INDEX IF NOT EXISTS idx_contacts_company ON proximal_contacts (company_id);
CREATE INDEX IF NOT EXISTS idx_contacts_email   ON proximal_contacts (email) WHERE email IS NOT NULL;


-- ── Step 5: Lead Output View ───────────────────────────────
-- This is the view the mobile app queries.
-- Joins company + best signal + best contact into a single row.
CREATE OR REPLACE VIEW lead_output AS
SELECT
    c.id              AS company_id,
    c.name            AS brand_name,
    c.domain,
    c.niche,
    c.company_size,
    -- Best signal (most recent event)
    s.signal_type,
    s.headline,
    s.summary         AS intent_summary,
    s.source_url      AS signal_source_url,
    s.event_date,
    -- Best contact (lowest proximity_rank, prefer verified email)
    pc.full_name      AS contact_name,
    pc.job_title      AS contact_title,
    pc.email          AS contact_email,
    pc.email_verified,
    pc.linkedin_url   AS contact_linkedin,
    pc.proximity_rank,
    -- Metadata
    c.created_at,
    -- Readiness score
    CASE
        WHEN pc.email IS NOT NULL AND pc.email_verified THEN 'ready'
        WHEN pc.email IS NOT NULL                       THEN 'unverified'
        WHEN pc.linkedin_url IS NOT NULL                THEN 'manual_linkedin'
        ELSE 'research_needed'
    END AS lead_status
FROM companies c
LEFT JOIN LATERAL (
    SELECT * FROM company_signals
    WHERE company_id = c.id
    ORDER BY event_date DESC NULLS LAST, created_at DESC
    LIMIT 1
) s ON true
LEFT JOIN LATERAL (
    SELECT * FROM proximal_contacts
    WHERE company_id = c.id
    ORDER BY proximity_rank ASC, email_verified DESC NULLS LAST, updated_at DESC
    LIMIT 1
) pc ON true
WHERE c.created_at > NOW() - INTERVAL '7 days';


-- ── Step 6: Semantic Search RPC ────────────────────────────
CREATE OR REPLACE FUNCTION match_signals(
    query_embedding vector(768),
    match_threshold float DEFAULT 0.7,
    match_count int DEFAULT 10
)
RETURNS TABLE (
    signal_id    BIGINT,
    company_name TEXT,
    headline     TEXT,
    summary      TEXT,
    similarity   float
)
LANGUAGE plpgsql
AS $$
BEGIN
    RETURN QUERY
    SELECT
        cs.id,
        c.name,
        cs.headline,
        cs.summary,
        1 - (cs.embedding <=> query_embedding) AS similarity
    FROM company_signals cs
    JOIN companies c ON c.id = cs.company_id
    WHERE 1 - (cs.embedding <=> query_embedding) > match_threshold
    ORDER BY cs.embedding <=> query_embedding
    LIMIT match_count;
END;
$$;
