-- ═══════════════════════════════════════════════════════════
-- Closr — Initial Database Schema (Supabase / PostgreSQL)
-- Run this in the Supabase SQL Editor to bootstrap the DB.
-- ═══════════════════════════════════════════════════════════

-- ── Daily Lead Pool ────────────────────────────────────────
-- This is the table the mobile app reads from every morning.
-- Leads older than 24h are cleared before each injection.
CREATE TABLE IF NOT EXISTS daily_pool (
    id              BIGSERIAL PRIMARY KEY,
    brand_name      TEXT NOT NULL,
    niche           TEXT,
    company_size    TEXT,
    intent_signal   TEXT,
    intent_tier     TEXT,
    confidence      REAL DEFAULT 0.0,
    icebreaker_pitch TEXT,
    contact_email   TEXT NOT NULL,
    domain          TEXT,
    source          TEXT,
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

-- PostgreSQL cannot use functions like DATE() inside a standard
-- UNIQUE constraint block. Declared as a separate unique index.
CREATE UNIQUE INDEX IF NOT EXISTS idx_unique_brand_date
    ON daily_pool (brand_name, ((created_at AT TIME ZONE 'UTC')::DATE));


-- ── Unenriched Leads ───────────────────────────────────────
-- Leads that passed LLM extraction + validation but could NOT
-- be enriched with a verified email. Stored for manual review.
CREATE TABLE IF NOT EXISTS unenriched_leads (
    id              BIGSERIAL PRIMARY KEY,
    brand_name      TEXT NOT NULL,
    niche           TEXT,
    intent_signal   TEXT,
    confidence      REAL DEFAULT 0.0,
    domain          TEXT,
    reason          TEXT,
    source          TEXT,
    raw_text        TEXT,
    created_at      TIMESTAMPTZ DEFAULT NOW()
);


-- ── Pipeline Run Log ───────────────────────────────────────
-- One row per orchestration run. Used for the /health endpoint
-- and for debugging throughput issues.
CREATE TABLE IF NOT EXISTS pipeline_runs (
    id              BIGSERIAL PRIMARY KEY,
    started_at      TIMESTAMPTZ NOT NULL,
    finished_at     TIMESTAMPTZ,
    scraped_count   INT DEFAULT 0,
    extracted_count INT DEFAULT 0,
    enriched_count  INT DEFAULT 0,
    injected_count  INT DEFAULT 0,
    errors          TEXT,
    status          TEXT DEFAULT 'running'
);


-- ── Enrichment Usage Tracker ───────────────────────────────
-- Tracks how many API calls have been consumed per enricher
-- for the current calendar month. Reset externally or via cron.
CREATE TABLE IF NOT EXISTS enrichment_usage (
    id              BIGSERIAL PRIMARY KEY,
    enricher_name   TEXT NOT NULL,
    month_year      TEXT NOT NULL,          -- e.g. "2026-04"
    usage_count     INT DEFAULT 0,
    UNIQUE (enricher_name, month_year)
);
