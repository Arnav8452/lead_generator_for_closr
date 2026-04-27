"""
Closr — Polite Deep Scraper
Primary: Jina Reader Free Tier (no API key needed)
Fallback: readability-lxml (local Firefox Reader View logic)
Safety: Hard 6000-char cap to prevent LLM OOM on 4GB VRAM.
"""

import logging
import re
import os
import hashlib

from bs4 import BeautifulSoup
from curl_cffi import requests as cffi_requests
from readability import Document
from googlenewsdecoder import gnewsdecoder
import requests

logger = logging.getLogger("closr.scrapers.polite_scraper")

# Root of the project
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Hard character cap — prevents Ollama OOM on 4GB VRAM
MAX_CHARS = 6000


def unroll_google_link(google_url):
    try:
        print(f"Decrypting Google Link: {google_url}")
        result = gnewsdecoder(google_url)
        
        if result.get("status"):
            real_url = result["decoded_url"]
            print(f"Successfully decrypted: {real_url}")
            return real_url
        else:
            print(f"Decryption failed, falling back to original: {result.get('message')}")
            return google_url
            
    except Exception as e:
        print(f"Decoder exception: {e}")
        return google_url


def check_for_updates(url: str, last_etag: str = None, last_modified: str = None) -> tuple[bool, str, str]:
    """
    Checks if a URL has new content by using a HEAD request.
    Returns (has_updated, new_etag, new_modified).
    """
    try:
        response = cffi_requests.head(url, impersonate="chrome120", timeout=10, verify=False)
        
        # Fallback to GET if HEAD is not allowed
        if response.status_code == 405:
            response = cffi_requests.get(url, impersonate="chrome120", timeout=10, verify=False)
            
        if response.status_code >= 400:
            print(f"Warning: Got status code {response.status_code} for {url}")
            return False, None, None

        new_etag = response.headers.get("ETag")
        new_modified = response.headers.get("Last-Modified")
        
        if new_etag and new_etag == last_etag:
            return False, new_etag, new_modified
            
        if new_modified and new_modified == last_modified:
            return False, new_etag, new_modified
            
        return True, new_etag, new_modified

    except Exception as e:
        print(f"Error checking HEAD for {url}: {e}")
        return False, None, None


def clean_and_truncate(text: str, max_chars: int = MAX_CHARS) -> str:
    """
    Standardized text cleaner for all scraper output.
    Collapses whitespace, strips garbage, and enforces a hard character cap
    to prevent LLM OOM crashes on low-VRAM GPUs.
    """
    if not text:
        return ""
    clean_text = re.sub(r'\s+', ' ', text).strip()
    if len(clean_text) > max_chars:
        # Keep head and tail — job title is often at top, location at bottom
        half = max_chars // 2
        return clean_text[:half] + "\n\n...[TRUNCATED]...\n\n" + clean_text[-half:]
    return clean_text


def _jina_scrape(url: str) -> str | None:
    """
    Jina Reader Free Tier — converts any URL to clean markdown.
    No API key required. Rate-limited but free.
    """
    try:
        res = requests.get(
            f"https://r.jina.ai/{url}",
            headers={"Accept": "text/plain"},
            timeout=15,
        )
        if res.status_code == 200 and len(res.text.strip()) > 100:
            logger.debug(f"Jina Reader success: {url}")
            return clean_and_truncate(res.text)
    except Exception as e:
        logger.debug(f"Jina Reader failed for {url}: {e}")
    return None


def _readability_scrape(url: str) -> str | None:
    """
    Local readability-lxml fallback — the same algorithm Firefox Reader View uses.
    Strips sidebars, navs, footers, and ads. Returns only the main article content.
    """
    try:
        response = cffi_requests.get(url, impersonate="chrome120", timeout=15, verify=False)
        if response.status_code != 200:
            logger.debug(f"Readability fetch failed ({response.status_code}): {url}")
            return None

        doc = Document(response.text)
        summary_html = doc.summary()

        # Convert the cleaned HTML summary to plain text
        soup = BeautifulSoup(summary_html, "lxml")
        
        # Extra safety: strip any remaining garbage tags
        for element in soup(["script", "style", "nav", "footer", "header", "aside", "noscript", "iframe"]):
            element.decompose()

        text = soup.get_text(separator=' ', strip=True)

        if len(text.strip()) > 100:
            logger.debug(f"Readability success: {url}")
            return clean_and_truncate(text)

    except Exception as e:
        logger.debug(f"Readability failed for {url}: {e}")

    return None


def chunk_by_words(text: str, max_words: int = 500) -> list[str]:
    words = text.split()
    chunks = []
    for i in range(0, len(words), max_words):
        chunk = " ".join(words[i:i + max_words])
        chunks.append(chunk)
    return chunks


def scrape_article(url: str) -> list[str]:
    """
    Deep scrape a URL for clean, LLM-ready text.
    
    Strategy:
      1. Jina Reader Free Tier (best quality, no key needed)
      2. readability-lxml local fallback (Firefox Reader View algorithm)
    
    Returns a list of text chunks (max 500 words each), or empty list on failure.
    """
    if not url:
        return []

    # Skip known non-article URLs (images, videos, PDFs)
    skip_extensions = ('.png', '.jpg', '.jpeg', '.gif', '.webp', '.svg', '.mp4', '.pdf')
    if any(url.lower().split('?')[0].endswith(ext) for ext in skip_extensions):
        logger.debug(f"Skipping non-article URL: {url}")
        return []

    # Strategy 1: Jina Reader
    text = _jina_scrape(url)

    # Strategy 2: Local Readability
    if not text:
        text = _readability_scrape(url)

    if text:
        return chunk_by_words(text, max_words=500)

    logger.debug(f"All scrape strategies failed for {url}")
    return []


if __name__ == "__main__":
    # Test the scraper
    test_url = "https://example.com"
    print(f"Testing scraper with {test_url}")
    
    is_updated, new_etag, new_mod = check_for_updates(test_url)
    print(f"Updated: {is_updated}, ETag: {new_etag}, Last-Modified: {new_mod}")
    
    if is_updated:
        text = scrape_article(test_url)
        if text:
            print("\nExtracted Text Snapshot:")
            print(text[0][:200] + "...")
