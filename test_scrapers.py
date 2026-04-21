import sys
import logging
from scrapers.google_news_funding import GoogleNewsFundingScraper
from scrapers.reddit_stealth import RedditStealthScraper
from scrapers.remote_boards import RemoteBoardsScraper
from scrapers.ats_jobs import ATSJobsScraper

logging.basicConfig(level=logging.WARNING)

scrapers = [
    GoogleNewsFundingScraper(),
    RedditStealthScraper(),
    RemoteBoardsScraper(),
    ATSJobsScraper()
]

for s in scrapers:
    try:
        leads = s.fetch()
        if leads:
            print(f"\n{'='*70}\n[SOURCE] {s.source_name} sample:\n{'-'*70}")
            print(leads[0].raw_text)
            print(f"{'='*70}\n")
    except Exception as e:
        pass
