"""
Closr — Local Embedding Generator (Ollama / nomic-embed-text)
Generates 768-dimensional dense vectors for semantic search via Supabase pgvector.
Runs entirely locally — zero cost, no API throttling.
"""

import logging

import requests

from config import OLLAMA_BASE_URL, OLLAMA_TIMEOUT

logger = logging.getLogger("closr.pipeline.embedding")

# Ollama embeddings endpoint
OLLAMA_EMBED_URL = f"{OLLAMA_BASE_URL}/api/embeddings"

# Model: nomic-embed-text produces 768-dim vectors,
# optimized for dense retrieval / semantic search.
EMBEDDING_MODEL = "nomic-embed-text"
EMBEDDING_DIMENSIONS = 768


def generate_embedding(text: str) -> list[float] | None:
    """
    Generate a 768-dimensional embedding vector for the given text
    using Ollama's local nomic-embed-text model.

    Args:
        text: The text to embed (typically a signal summary, 2-3 sentences).

    Returns:
        A list of 768 floats, or None if generation fails.
    """
    if not text or not text.strip():
        logger.warning("Embedding: Empty text provided — skipping.")
        return None

    try:
        response = requests.post(
            OLLAMA_EMBED_URL,
            json={
                "model": EMBEDDING_MODEL,
                "prompt": text.strip(),
            },
            timeout=OLLAMA_TIMEOUT,
        )
        response.raise_for_status()
        data = response.json()

        embedding = data.get("embedding")
        if not embedding:
            logger.warning("Embedding: Ollama returned no embedding vector.")
            return None

        if len(embedding) != EMBEDDING_DIMENSIONS:
            logger.warning(
                f"Embedding: Expected {EMBEDDING_DIMENSIONS} dims, "
                f"got {len(embedding)}. Model mismatch?"
            )
            # Still return it — pgvector will reject if dim mismatch
            return embedding

        return embedding

    except requests.exceptions.ConnectionError:
        logger.error(
            f"Embedding: Cannot connect to Ollama at {OLLAMA_BASE_URL}. "
            f"Is Ollama running? Has nomic-embed-text been pulled?"
        )
        return None
    except requests.exceptions.Timeout:
        logger.warning("Embedding: Ollama request timed out.")
        return None
    except requests.exceptions.RequestException as e:
        logger.error(f"Embedding: Request error: {e}")
        return None

def generate_embeddings_batch(texts: list[str]) -> list[list[float]] | None:
    """
    Generate multiple embeddings in a single GPU batch using Ollama's /api/embed.
    """
    valid_texts = [t.strip() for t in texts if t and t.strip()]
    if not valid_texts:
        return []

    try:
        response = requests.post(
            f"{OLLAMA_BASE_URL}/api/embed",
            json={
                "model": EMBEDDING_MODEL,
                "input": valid_texts,
            },
            timeout=OLLAMA_TIMEOUT,
        )
        response.raise_for_status()
        data = response.json()

        embeddings = data.get("embeddings")
        if not embeddings:
            logger.warning("Batch Embedding: Ollama returned no embeddings.")
            return None

        return embeddings

    except Exception as e:
        logger.error(f"Batch Embedding Error: {e}")
        return None
