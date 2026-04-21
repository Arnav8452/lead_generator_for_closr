import logging
from db.supabase_client import _get_client

logger = logging.getLogger(__name__)

class PatternDB:
    def lookup(self, domain: str, decision_maker_name: str) -> str | None:
        """
        Attempts to reconstruct an email using historically verified patterns.
        Fails silently and returns None if the pattern DB is empty or missing.
        """
        if not domain or not decision_maker_name or str(decision_maker_name).lower() == "null":
            return None

        try:
            client = _get_client()
            # Check if we have a verified pattern for this domain
            res = client.table("domain_patterns").select("pattern").eq("domain", domain).execute()
            
            if not res.data:
                return None
                
            pattern = res.data[0].get("pattern")
            parts = decision_maker_name.strip().lower().split()
            first = parts[0]
            last = parts[-1] if len(parts) > 1 else ""
            
            # Apply the pattern
            if pattern == "first":
                return f"{first}@{domain}"
            elif pattern == "first.last":
                return f"{first}.{last}@{domain}"
            elif pattern == "f_last":
                return f"{first[0]}{last}@{domain}"
                
            return None

        except Exception as e:
            logger.debug(f"Pattern DB lookup bypassed/failed: {e}")
            return None

pattern_db = PatternDB()
