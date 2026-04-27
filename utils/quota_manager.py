import json
import os
import logging
import hashlib
from datetime import date
from config import (
    HUNTER_API_KEYS, SNOV_CREDENTIALS, PROSPEO_API_KEYS, APOLLO_API_KEYS,
    SERPER_API_KEYS, GOOGLE_SEARCH_API_KEYS, GOOGLE_SEARCH_CXS
)

logger = logging.getLogger("closr.utils.quota_manager")

QUOTA_FILE = "api_quotas.json"

class QuotaManager:
    def __init__(self):
        # API limits updated to match current free tiers
        self.limits = {
            "serper": 2500,
            "hunter": 50,
            "snov": 50,
            "prospeo": 100,
            "apollo": 75,
            "google_search": 100  # 100 per day
        }
        
        # Load persistent tracking from local file
        self._usage_cache = self._load_quotas()

    def _hash_key(self, key: str) -> str:
        """Create a short hash of the key to use as the tracking ID"""
        return hashlib.md5(key.encode()).hexdigest()[:8]

    def _load_quotas(self) -> dict:
        """Read the quotas from the local JSON file."""
        if os.path.exists(QUOTA_FILE):
            try:
                with open(QUOTA_FILE, "r") as f:
                    return json.load(f)
            except Exception as e:
                logger.error(f"Failed to load {QUOTA_FILE}: {e}")
        
        # If the file doesn't exist or fails to load, start fresh
        return {}

    def _save_quotas(self):
        """Write the current cache back to the local JSON file."""
        try:
            with open(QUOTA_FILE, "w") as f:
                json.dump(self._usage_cache, f, indent=4)
        except Exception as e:
            logger.error(f"Failed to save {QUOTA_FILE}: {e}")

    def can_use(self, tracking_id: str, api_type: str) -> bool:
        current_usage = self._usage_cache.get(tracking_id, 0)
        limit = self.limits.get(api_type, 0)
        return current_usage < limit

    def consume(self, tracking_id: str):
        # Update memory
        self._usage_cache[tracking_id] = self._usage_cache.get(tracking_id, 0) + 1
        # Persist immediately to file to prevent cross-run amnesia
        self._save_quotas()

    # ── Key Rotation Methods ──────────────────────────────────────────────────
    def get_prospeo_key(self) -> str | None:
        for key in PROSPEO_API_KEYS:
            if not key: continue
            tracking_id = f"prospeo_{self._hash_key(key)}"
            if self.can_use(tracking_id, "prospeo"):
                return key
        logger.warning("QUOTA EXHAUSTED: All Prospeo keys are out of credits.")
        return None

    def get_snov_credentials(self) -> tuple[str, str] | None:
        """Returns (client_id, client_secret) for the first available Snov account."""
        for cred_pair in SNOV_CREDENTIALS:
            if ":" not in cred_pair: continue
            client_id, client_secret = cred_pair.split(":", 1)
            tracking_id = f"snov_{self._hash_key(client_id)}"
            if self.can_use(tracking_id, "snov"):
                return client_id, client_secret
        logger.warning("QUOTA EXHAUSTED: All Snov credentials are out of credits.")
        return None

    def get_hunter_key(self) -> str | None:
        for key in HUNTER_API_KEYS:
            if not key: continue
            tracking_id = f"hunter_{self._hash_key(key)}"
            if self.can_use(tracking_id, "hunter"):
                return key
        logger.warning("QUOTA EXHAUSTED: All Hunter keys are out of credits.")
        return None

    def get_apollo_key(self) -> str | None:
        for key in APOLLO_API_KEYS:
            if not key: continue
            tracking_id = f"apollo_{self._hash_key(key)}"
            if self.can_use(tracking_id, "apollo"):
                return key
        logger.warning("QUOTA EXHAUSTED: All Apollo keys are out of credits.")
        return None
        
    def get_serper_key(self) -> str | None:
        for key in SERPER_API_KEYS:
            if not key: continue
            tracking_id = f"serper_{self._hash_key(key)}"
            if self.can_use(tracking_id, "serper"):
                return key
        logger.warning("QUOTA EXHAUSTED: All Serper keys are out of credits.")
        return None

    def get_google_search_credentials(self) -> tuple[str, str] | None:
        """
        Returns (api_key, cx) for the first available Google Custom Search account.
        Google quotas reset daily, so we append the current date to the hash.
        """
        today = date.today().isoformat()
        
        # We assume GOOGLE_SEARCH_API_KEYS and GOOGLE_SEARCH_CXS are 1:1 paired
        for key, cx in zip(GOOGLE_SEARCH_API_KEYS, GOOGLE_SEARCH_CXS):
            if not key or not cx: continue
            
            # Hash includes both the key and the current date (e.g. google_abc123_2026-04-26)
            # This ensures tomorrow the quota automatically starts fresh at 0.
            tracking_id = f"google_{self._hash_key(key)}_{today}"
            if self.can_use(tracking_id, "google_search"):
                return key, cx
                
        logger.warning("QUOTA EXHAUSTED: All Google Search keys are out of credits for today.")
        return None

    def consume_google_search(self, key: str):
        today = date.today().isoformat()
        self.consume(f"google_{self._hash_key(key)}_{today}")

    def consume_serper(self, key: str):
        self.consume(f"serper_{self._hash_key(key)}")

    def consume_hunter(self, key: str):
        self.consume(f"hunter_{self._hash_key(key)}")

    def consume_apollo(self, key: str):
        self.consume(f"apollo_{self._hash_key(key)}")

    def consume_prospeo(self, key: str):
        self.consume(f"prospeo_{self._hash_key(key)}")

    def consume_snov(self, client_id: str):
        self.consume(f"snov_{self._hash_key(client_id)}")

quota_manager = QuotaManager()