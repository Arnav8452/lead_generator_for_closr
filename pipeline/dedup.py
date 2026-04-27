"""
Closr — V2 Resolution Gauntlet (RAM Deduplication)

Runs once per pipeline micro-batch, AFTER all LLM extractions are collected
and BEFORE any enrichment API calls are made. This ensures you never call
Hunter/Snov twice for "Acme Corp" and "Acme Corp Inc".

Three-stage pipeline in order of cost (cheapest first):

  Stage 1 — Domain Grouping   : pandas groupby on resolved domain (O(N log N))
  Stage 2 — String Locks      : Jellyfish Jaro-Winkler + Anti-Hierarchy Lock
  Stage 3 — Vector Scalpel    : all-MiniLM-L6-v2 cosine for domain-less entities

Each stage only processes what the previous stage didn't already resolve.
"""

import logging
import re

import jellyfish

from config import (
    GAUNTLET_JARO_THRESHOLD,
    GAUNTLET_VECTOR_COSINE_THRESHOLD,
    SNIPER_MODEL,
)

logger = logging.getLogger("closr.pipeline.dedup")

# ─────────────────────────────────────────────────────────
# Title Seniority Tiers for Anti-Hierarchy Lock
# Two contacts at different tiers = distinct entities, never merge.
# ─────────────────────────────────────────────────────────
_SENIORITY_TIERS: list[set[str]] = [
    {"intern", "coordinator", "associate", "assistant", "specialist"},
    {"manager", "lead", "senior"},
    {"director", "head", "vp", "vice president"},
    {"cmo", "ceo", "cto", "coo", "cro", "founder", "co-founder", "president", "owner"},
]


def _get_seniority_tier(title: str) -> int:
    """Return tier index 0-3, or -1 if title is unknown/empty."""
    t = (title or "").lower()
    for i, keywords in enumerate(_SENIORITY_TIERS):
        if any(kw in t for kw in keywords):
            return i
    return -1


def _normalize_company(name: str) -> str:
    """Lowercase, strip legal suffixes and punctuation for comparison."""
    if not name:
        return ""
    n = re.sub(
        r'\b(inc\.?|llc\.?|ltd\.?|co\.?|corp\.?|labs?\.?|group|holdings?|technologies|tech)\b',
        "", name.lower(),
    )
    n = re.sub(r'[^a-z0-9\s]', "", n)
    return re.sub(r'\s+', " ", n).strip()


def _first_token(name: str) -> str:
    """Return lowercase first word of a string."""
    parts = (name or "").strip().split()
    return parts[0].lower() if parts else ""


def _top_contact_title(item: dict) -> str:
    """Return the job title of the first contact in an extracted entity."""
    contacts = item.get("entity", {}).get("contacts", [])
    return contacts[0].get("title", "") if contacts else ""


def _contact_count(item: dict) -> int:
    return len(item.get("entity", {}).get("contacts", []))


class ResolutionGauntlet:
    """
    Deduplicates a batch of LLM-extracted entity dicts in RAM.

    Input:  list[dict]  — each dict is the 'extracted' payload from llm_worker
    Output: list[dict]  — deduplicated, one entry per unique company entity
    """

    def __init__(self):
        self._sniper_model = None

    def _load_sniper(self) -> None:
        if self._sniper_model is not None:
            return
        try:
            from sentence_transformers import SentenceTransformer
            self._sniper_model = SentenceTransformer(SNIPER_MODEL, device="cpu")
            logger.info("Gauntlet: Vector Scalpel model loaded.")
        except Exception as e:
            logger.warning(f"Gauntlet: Could not load Sniper model — {e}. Vector Scalpel disabled.")

    # ─────────────────────────────────────────────────────
    # Public API
    # ─────────────────────────────────────────────────────
    def run(self, extracted_list: list[dict]) -> list[dict]:
        """Execute all three gauntlet stages and return deduplicated list."""
        if not extracted_list:
            return []

        before = len(extracted_list)

        deduped = self._domain_grouping(extracted_list)
        deduped = self._string_locks(deduped)
        deduped = self._vector_scalpel(deduped)

        after = len(deduped)
        logger.info(
            f"Resolution Gauntlet: {before} → {after} entities "
            f"({before - after} merged/dropped)"
        )
        return deduped

    # ─────────────────────────────────────────────────────
    # Stage 1: Domain Grouping
    # ─────────────────────────────────────────────────────
    def _domain_grouping(self, extracted_list: list[dict]) -> list[dict]:
        """
        Group by resolved domain. Keep the record with the most contacts per domain.
        Entities without a domain pass through unchanged.
        """
        with_domain: dict[str, dict] = {}
        without_domain: list[dict] = []

        for item in extracted_list:
            domain = (item.get("company_data") or {}).get("domain")
            if domain:
                existing = with_domain.get(domain)
                if existing is None:
                    with_domain[domain] = item
                else:
                    # Keep whichever record has more contacts
                    if _contact_count(item) > _contact_count(existing):
                        with_domain[domain] = item
                    logger.debug(
                        f"Gauntlet (domain): merged duplicate '{item.get('company_name')}' "
                        f"→ '{existing.get('company_name')}' (domain={domain})"
                    )
            else:
                without_domain.append(item)

        return list(with_domain.values()) + without_domain

    # ─────────────────────────────────────────────────────
    # Stage 2: String Locks
    # ─────────────────────────────────────────────────────
    def _string_locks(self, extracted_list: list[dict]) -> list[dict]:
        """
        Jaro-Winkler fuzzy match on normalized company name,
        guarded by First-Token Lock and Anti-Hierarchy Lock.
        """
        kept: list[dict] = []

        for item in extracted_list:
            company_name = item.get("company_name", "")
            norm_name = _normalize_company(company_name)
            if not norm_name:
                kept.append(item)
                continue

            is_duplicate = False
            for existing in kept:
                existing_norm = _normalize_company(existing.get("company_name", ""))

                # First-Token Lock: "Glossier Beauty" vs "Glossy Labs" — first words differ
                if _first_token(norm_name) != _first_token(existing_norm):
                    continue

                # Jaro-Winkler similarity threshold
                jaro = jellyfish.jaro_winkler_similarity(norm_name, existing_norm)
                if jaro < GAUNTLET_JARO_THRESHOLD:
                    continue

                # Anti-Hierarchy Lock: different seniority = distinct leads
                new_tier = _get_seniority_tier(_top_contact_title(item))
                exist_tier = _get_seniority_tier(_top_contact_title(existing))
                if new_tier != -1 and exist_tier != -1 and new_tier != exist_tier:
                    # Different tiers — same company but different decision-maker level
                    # Keep both as distinct enrichment targets
                    continue

                # All locks passed → duplicate
                logger.debug(
                    f"Gauntlet (string): merged '{company_name}' "
                    f"→ '{existing.get('company_name')}' (jaro={jaro:.2f})"
                )
                is_duplicate = True
                break

            if not is_duplicate:
                kept.append(item)

        return kept

    # ─────────────────────────────────────────────────────
    # Stage 3: Vector Scalpel
    # ─────────────────────────────────────────────────────
    def _vector_scalpel(self, extracted_list: list[dict]) -> list[dict]:
        """
        For entities still lacking a domain after String Locks,
        encode [name + title + company] fingerprints and merge pairs
        with cosine similarity > threshold.
        Entities with a domain are already clean from Stage 1.
        """
        with_domain = [i for i in extracted_list if (i.get("company_data") or {}).get("domain")]
        no_domain = [i for i in extracted_list if not (i.get("company_data") or {}).get("domain")]

        if len(no_domain) <= 1:
            return extracted_list

        self._load_sniper()
        if self._sniper_model is None:
            return extracted_list

        try:
            import numpy as np

            def _fingerprint(item: dict) -> str:
                contacts = item.get("entity", {}).get("contacts", [])
                top_name = contacts[0].get("name", "") if contacts else ""
                top_title = contacts[0].get("title", "") if contacts else ""
                company = item.get("company_name", "")
                return f"{top_name} {top_title} {company}".strip()

            fingerprints = [_fingerprint(i) for i in no_domain]
            embeddings = self._sniper_model.encode(
                fingerprints, normalize_embeddings=True, show_progress_bar=False
            )
            # Full pairwise cosine similarity matrix
            sim_matrix = embeddings @ embeddings.T

            merged: set[int] = set()
            kept: list[dict] = []

            for i, item in enumerate(no_domain):
                if i in merged:
                    continue
                kept.append(item)
                for j in range(i + 1, len(no_domain)):
                    if j not in merged and sim_matrix[i, j] >= GAUNTLET_VECTOR_COSINE_THRESHOLD:
                        logger.debug(
                            f"Gauntlet (vector): merged '{no_domain[j].get('company_name')}' "
                            f"→ '{item.get('company_name')}' (cosine={sim_matrix[i, j]:.2f})"
                        )
                        merged.add(j)

            return with_domain + kept

        except Exception as e:
            logger.warning(f"Gauntlet (vector scalpel) error — {e}. Skipping.")
            return extracted_list
