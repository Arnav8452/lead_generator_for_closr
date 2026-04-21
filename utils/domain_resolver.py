import logging
from curl_cffi import requests

logger = logging.getLogger(__name__)

def resolve_domain(brand_name: str) -> str | None:
    """Uses Clearbit's free autocomplete API for accurate firmographic resolution."""
    if not brand_name or str(brand_name).lower() in ["null", "none"]:
        return None
        
    url = f"https://autocomplete.clearbit.com/v1/companies/suggest?query={brand_name}"
    
    try:
        res = requests.get(url, timeout=5, impersonate="chrome120")
        if res.status_code == 200:
            data = res.json()
            if data and len(data) > 0:
                return data[0].get("domain")
    except Exception as e:
        logger.debug(f"Domain resolution failed for {brand_name}: {e}")
        
    return None
