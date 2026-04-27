import os
import logging
from dotenv import load_dotenv

# Load env vars before importing config
load_dotenv()

from config import SUPABASE_URL, SUPABASE_KEY
from supabase import create_client
from pipeline.tools import normalize_title, assign_proximity_rank

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")
logger = logging.getLogger("clean_db")

def clean_database():
    if not SUPABASE_URL or not SUPABASE_KEY:
        logger.error("Missing Supabase credentials.")
        return

    client = create_client(SUPABASE_URL, SUPABASE_KEY)
    
    try:
        # 1. Wipe all unenriched contacts
        logger.info("Wiping all contacts where email is NULL...")
        result = client.table("proximal_contacts").delete().is_("email", "null").execute()
        deleted_count = len(result.data) if result.data else 0
        logger.info(f"✓ Wiped {deleted_count} unenriched contacts.")
        
        # 2. Fix the titles/ranks of the enriched contacts we are keeping
        logger.info("Normalizing job titles for the saved enriched contacts...")
        enriched = client.table("proximal_contacts").select("*, companies(name)").not_.is_("email", "null").execute()
        
        updated_count = 0
        if enriched.data:
            for row in enriched.data:
                c_id = row.get("id")
                raw_title = row.get("job_title", "")
                
                # Extract company name from the joined table
                company_obj = row.get("companies")
                company_name = company_obj.get("name", "") if company_obj else ""
                
                new_title = normalize_title(raw_title, company_name)
                new_rank = assign_proximity_rank(new_title)
                
                # Only update if there is a change
                if new_title != raw_title or new_rank != row.get("proximity_rank"):
                    client.table("proximal_contacts").update({
                        "job_title": new_title,
                        "proximity_rank": new_rank
                    }).eq("id", c_id).execute()
                    updated_count += 1
                    logger.info(f"  Fixed contact ID {c_id}: '{raw_title}' -> '{new_title}' (Rank: {new_rank})")
                    
        logger.info(f"✓ Successfully updated {updated_count} enriched contacts with normalized titles.")
        logger.info("Database is clean and ready for the next pipeline run!")
        
    except Exception as e:
        logger.error(f"Error cleaning database: {e}")

if __name__ == "__main__":
    clean_database()
