"""
Closr — V2 Extractor: Air-Lock + Lexical Pulse + Sniper

Three-stage defensive filter sitting between raw scraper output and the LLM.
Kills irrelevant leads before expensive DOM fetches or Qwen calls happen.

Stage 1 — Air-Lock (bart-large-mnli, CPU):
    Zero-shot classifies the RSS headline/title against TARGET_SIGNALS.
    Confidence < 0.55 → lead dropped. DOM never fetched.

Stage 2 — Lexical Pulse Check (O(N) deque):
    Sliding window over clean DOM text. Requires ≥ 2 capitalized entities
    near TRIGGER_VERBS. Kills generic PR fluff instantly.

Stage 3 — Sniper (all-MiniLM-L6-v2, CPU):
    Chunks DOM Markdown into 300-word/50-word-overlap segments.
    Encodes all chunks, drops those with cosine < 0.35 vs the Intent Vector
    (centroid of TARGET_SIGNALS embeddings, pre-compiled at startup).
    Only surviving high-density chunks go to Qwen.

Models are lazy-loaded once on first call, then cached — no per-lead overhead.
All models run on CPU. Qwen 7B owns the GPU.
"""

import logging
import re
from collections import deque

from config import (
    AIRLOCK_MODEL,
    AIRLOCK_CONFIDENCE_THRESHOLD,
    TARGET_SIGNALS,
    SNIPER_MODEL,
    SNIPER_CHUNK_SIZE,
    SNIPER_CHUNK_OVERLAP,
    SNIPER_COSINE_THRESHOLD,
)

logger = logging.getLogger("closr.pipeline.extractor")

# ─────────────────────────────────────────────────────────
# Lexical Pulse: Trigger Verbs
# ─────────────────────────────────────────────────────────
TRIGGER_VERBS: set[str] = {
    # C-Suite & Leadership
    "ceo", "cmo", "cro", "cto", "vp", "director", "founder", "head",
    # Hiring & Movement
    "joined", "appointed", "hired", "promoted", "named", "welcomes",
    "looking", "seeking", "searching", "hiring", "onboarding", "recruiting",
    # Financial & M&A
    "raised", "acquired", "funded", "invested", "backed", "secured", "merged",
    "raises", "acquires", "funds",
    # Deals & Agency
    "sponsored", "partnered", "signed", "onboarded", "retained", "selected",
    # Product & Growth
    "launched", "released", "expanding", "announcing", "scaling", "growing", "building",
    # Needs & Intent
    "needs", "need", "want", "wants", "require", "requires", "planning",
}

# Proper-noun pattern: single capitalized word (e.g., "Glossier", "Sequoia")
_CAPS_PATTERN = re.compile(r'^[A-Z][a-z]{1,}$')


class Extractor:
    """
    Lazy-initialized singleton. Models load on first call to filter().
    Designed to be instantiated once at module level.
    """

    def __init__(self):
        self._airlock_pipe = None
        self._sniper_model = None
        self._intent_vector = None
        self._models_loaded = False

    # ─────────────────────────────────────────────────────
    # Model Loading
    # ─────────────────────────────────────────────────────
    def _load_models(self) -> None:
        """Load both models once. Safe to call multiple times."""
        if self._models_loaded:
            return

        logger.info("Extractor: Loading models (CPU only — VRAM reserved for Qwen)…")

        # Air-Lock: bart-large-mnli
        try:
            from transformers import pipeline as hf_pipeline
            self._airlock_pipe = hf_pipeline(
                "zero-shot-classification",
                model=AIRLOCK_MODEL,
                device=-1,          # Force CPU
            )
            logger.info("Extractor: Air-Lock (bart-large-mnli) loaded on CPU.")
        except Exception as e:
            logger.error(f"Extractor: Failed to load Air-Lock model — {e}. Failing open.")
            self._airlock_pipe = None

        # Sniper: all-MiniLM-L6-v2 + Intent Vector
        try:
            import numpy as np
            from sentence_transformers import SentenceTransformer
            self._sniper_model = SentenceTransformer(SNIPER_MODEL, device="cpu")
            # Pre-encode intent vector: centroid of all TARGET_SIGNALS embeddings
            signal_embeddings = self._sniper_model.encode(
                TARGET_SIGNALS, normalize_embeddings=True, show_progress_bar=False
            )
            self._intent_vector = np.mean(signal_embeddings, axis=0)
            # Re-normalize the centroid
            norm = np.linalg.norm(self._intent_vector)
            if norm > 0:
                self._intent_vector = self._intent_vector / norm
            logger.info("Extractor: Sniper (all-MiniLM-L6-v2) loaded. Intent vector compiled.")
        except Exception as e:
            logger.error(f"Extractor: Failed to load Sniper model — {e}. Failing open.")
            self._sniper_model = None
            self._intent_vector = None

        self._models_loaded = True

    # ─────────────────────────────────────────────────────
    # Stage 1: Air-Lock
    # ─────────────────────────────────────────────────────
    def airlock(self, title: str) -> bool:
        """
        Zero-shot classify the headline/title.
        Returns True (pass) or False (kill).
        Fails open if the model is unavailable.
        """
        if not title or not title.strip():
            return False

        self._load_models()

        if self._airlock_pipe is None:
            logger.debug("Extractor: Air-Lock unavailable — passing lead through.")
            return True

        try:
            result = self._airlock_pipe(
                title.strip()[:512],       # bart max input length
                candidate_labels=TARGET_SIGNALS,
                multi_label=True,
            )
            top_score = max(result["scores"])
            passes = top_score >= AIRLOCK_CONFIDENCE_THRESHOLD

            if passes:
                logger.debug(f"Extractor: Air-Lock PASS (score={top_score:.2f}): {title[:60]}")
            else:
                logger.info(f"Extractor: Air-Lock DROP (score={top_score:.2f}): {title[:60]}")

            return passes

        except Exception as e:
            logger.warning(f"Extractor: Air-Lock error — {e}. Passing lead through.")
            return True

    # ─────────────────────────────────────────────────────
    # Stage 2: Lexical Pulse Check
    # ─────────────────────────────────────────────────────
    def lexical_pulse(self, text: str) -> bool:
        """
        O(N) deque sliding window over text words.
        Returns True if ≥ 2 capitalized entities found near a TRIGGER_VERB.
        Returns True for short texts (< 50 words) — don't kill short posts.
        """
        if not text:
            return False

        self._load_models()

        words = text.split()

        # Short posts (Reddit one-liners, HN comments): pass through
        if len(words) < 50:
            return True

        window: deque[str] = deque(maxlen=15)

        for word in words:
            clean = word.lower().strip('.,!?;:()"\'')
            window.append(word)

            if clean in TRIGGER_VERBS:
                caps_count = sum(1 for w in window if _CAPS_PATTERN.match(w))
                if caps_count >= 2:
                    return True

        logger.debug(
            f"Extractor: Lexical Pulse DROP — no trigger+entity combo in "
            f"{len(words)}-word text."
        )
        return False

    # ─────────────────────────────────────────────────────
    # Stage 3: Sniper
    # ─────────────────────────────────────────────────────
    def sniper(self, dom_text: str) -> list[str]:
        """
        Encode DOM chunks via all-MiniLM-L6-v2 and drop those below
        the cosine threshold vs the pre-compiled Intent Vector.

        Returns surviving chunks, or ALL chunks if model unavailable.
        Always returns at least 1 chunk (first chunk as fallback).
        """
        chunks = self._chunk_text(dom_text)
        if not chunks:
            return []

        self._load_models()

        if self._sniper_model is None or self._intent_vector is None:
            logger.debug("Extractor: Sniper unavailable — returning all chunks.")
            return chunks

        try:
            import numpy as np
            embeddings = self._sniper_model.encode(
                chunks, normalize_embeddings=True, show_progress_bar=False
            )
            # Cosine similarity = dot product of normalized vectors
            scores = embeddings @ self._intent_vector
            surviving = [
                chunk for chunk, score in zip(chunks, scores)
                if score >= 0.25  # drop chunks below this (was 0.35, lowered to recover job postings)
            ]
            dropped = len(chunks) - len(surviving)
            if dropped:
                logger.debug(
                    f"Extractor: Sniper dropped {dropped}/{len(chunks)} chunks "
                    f"(threshold={SNIPER_COSINE_THRESHOLD})."
                )
            # Always keep at least 1 chunk — the first (usually most information-dense)
            return surviving if surviving else [chunks[0]]

        except Exception as e:
            logger.warning(f"Extractor: Sniper error — {e}. Returning all chunks.")
            return chunks

    # ─────────────────────────────────────────────────────
    # Public API
    # ─────────────────────────────────────────────────────
    def filter(self, title: str, dom_text: str = "") -> list[str] | None:
        """
        Run the full 3-stage pipeline.

        Args:
            title:    RSS headline or article title (Air-Lock input).
            dom_text: Clean Markdown from polite_scraper (Lexical + Sniper input).
                      Pass "" if the DOM was not yet fetched (Air-Lock-only mode).

        Returns:
            List of surviving high-density text chunks for LLM injection,
            or None if the lead is killed at any stage.
        """
        self._load_models()

        # ── Stage 1: Air-Lock ───────────────────────────
        if not self.airlock(title):
            return None

        # ── Stage 2: Lexical Pulse ──────────────────────
        combined = f"{title}\n{dom_text}" if dom_text else title
        if not self.lexical_pulse(combined):
            return None

        # ── Stage 3: Sniper ─────────────────────────────
        if dom_text:
            surviving_chunks = self.sniper(dom_text)
        else:
            # Air-Lock-only mode: no DOM yet — return title as single chunk
            surviving_chunks = [title]

        return surviving_chunks if surviving_chunks else None

    # ─────────────────────────────────────────────────────
    # Text Chunking Utility
    # ─────────────────────────────────────────────────────
    @staticmethod
    def _chunk_text(
        text: str,
        chunk_size: int = SNIPER_CHUNK_SIZE,
        overlap: int = SNIPER_CHUNK_OVERLAP,
    ) -> list[str]:
        """Split text into overlapping word-count chunks."""
        if not text:
            return []
        words = text.split()
        if len(words) <= chunk_size:
            return [text]
        step = max(chunk_size - overlap, 1)
        chunks = []
        for i in range(0, len(words), step):
            chunk = " ".join(words[i : i + chunk_size])
            if chunk:
                chunks.append(chunk)
        return chunks


# ── Module-level singleton ───────────────────────────────────────────────────
extractor = Extractor()
