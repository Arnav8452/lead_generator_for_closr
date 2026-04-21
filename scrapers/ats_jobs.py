"""
Closr — ATS Job Board Scraper
Combines three sources to find companies actively hiring creator/influencer
marketing roles:
  A) Jobspy (LinkedIn, Indeed, Glassdoor aggregation)
  B) Public Greenhouse board APIs
  C) Public Lever board APIs

Uses only free, public APIs — no RapidAPI or paid aggregators.
"""

import logging
from typing import Optional

from config import (
    SCRAPER_TIMEOUT,
    JOBSPY_RESULTS_WANTED,
    JOBSPY_HOURS_OLD,
)
from scrapers.base import BaseScraper, RawLead

logger = logging.getLogger("closr.scrapers.ats_jobs")

# ── Jobspy Configuration ──────────────────────────────────
JOBSPY_SEARCH_QUERY = '"influencer marketing" OR "UGC creator" OR "creator partnerships"'
JOBSPY_SITES = ["linkedin", "indeed"]

# ── Public ATS Board Slugs ────────────────────────────────
# Companies known to use Greenhouse/Lever with creator-economy roles.
# These are public board slugs — no authentication required.
GREENHOUSE_SLUGS = [
    "glossier", "hims", "thereareal", "allbirds", "warbyparker",
    "casper", "bombas", "away", "outdoorvoices", "renttherunway",
]

LEVER_SLUGS = [
    "goop", "fabletics", "nativecos", "quip", "brooklinen",
    "ritual", "hungryroot", "harrys", "thirdlove", "rothy",
]

# Keywords that indicate creator/influencer marketing roles
CREATOR_JOB_KEYWORDS = [
    "influencer", "creator", "ugc", "ambassador",
    "partnerships", "talent", "content creator",
]


class ATSJobsScraper(BaseScraper):
    source_name = "ats_jobs"

    def fetch(self) -> list[RawLead]:
        """
        Aggregate creator marketing job leads from Jobspy, Greenhouse,
        and Lever. Each source is attempted independently — failures in
        one don't block the others.
        """
        leads: list[RawLead] = []

        # Source A: Jobspy aggregation
        try:
            leads.extend(self._fetch_jobspy())
        except Exception as e:
            logger.warning(f"Jobspy scraping failed: {e}")

        # Source B: Public Greenhouse APIs
        for slug in GREENHOUSE_SLUGS:
            try:
                leads.extend(self._fetch_greenhouse(slug))
            except Exception as e:
                logger.warning(f"Greenhouse [{slug}] failed: {e}")

        # Source C: Public Lever APIs
        for slug in LEVER_SLUGS:
            try:
                leads.extend(self._fetch_lever(slug))
            except Exception as e:
                logger.warning(f"Lever [{slug}] failed: {e}")

        logger.info(f"ATS Jobs: {len(leads)} creator-role leads found total")
        return leads

    def _fetch_jobspy(self) -> list[RawLead]:
        """
        Use the python-jobspy library to search for creator marketing roles
        across LinkedIn, Indeed, and Glassdoor.
        """
        results: list[RawLead] = []
        try:
            from jobspy import scrape_jobs

            jobs = scrape_jobs(
                site_name=JOBSPY_SITES,
                search_term=JOBSPY_SEARCH_QUERY,
                results_wanted=JOBSPY_RESULTS_WANTED,
                hours_old=JOBSPY_HOURS_OLD,
                country_indeed="USA",
            )

            if jobs is not None and not jobs.empty:
                # URGENT FIX: Drop any job that doesn't list a company name
                jobs = jobs.dropna(subset=['company'])
                
                # Convert any remaining NaN values (like empty descriptions) to blank strings
                jobs = jobs.fillna("")

                for _, row in jobs.iterrows():
                    title = str(row.get("title", ""))
                    company = str(row.get("company", ""))
                    description = str(row.get("description", ""))[:500]
                    job_url = str(row.get("job_url", ""))

                    raw_text = (
                        f"Company: {company}\n"
                        f"Job Title: {title}\n"
                        f"Description: {description}"
                    )

                    results.append(
                        RawLead(
                            source=f"{self.source_name}_jobspy",
                            raw_text=raw_text,
                            url=job_url,
                            brand_name_hint=company if company else None,
                        )
                    )

            logger.info(f"Jobspy: {len(results)} jobs found")
        except ImportError:
            logger.error(
                "python-jobspy is not installed. "
                "Run: pip install python-jobspy"
            )
        except Exception as e:
            logger.error(f"Jobspy scraping error: {e}")
            raise

        return results

    def _fetch_greenhouse(self, slug: str) -> list[RawLead]:
        """
        Query a company's public Greenhouse job board API for creator roles.
        URL pattern: https://boards-api.greenhouse.io/v1/boards/{slug}/jobs
        """
        url = f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs"
        response = self.session.get(url, timeout=SCRAPER_TIMEOUT)

        if response.status_code == 404:
            logger.debug(f"Greenhouse board '{slug}' not found (404)")
            return []

        response.raise_for_status()
        data = response.json()
        jobs = data.get("jobs", [])
        results: list[RawLead] = []

        for job in jobs:
            title = job.get("title", "").lower()

            # Filter for creator/influencer marketing roles only
            if not any(kw in title for kw in CREATOR_JOB_KEYWORDS):
                continue

            location = job.get("location", {}).get("name", "Remote")
            job_url = job.get("absolute_url", url)

            raw_text = (
                f"Company: {slug.replace('-', ' ').title()}\n"
                f"Job Title: {job.get('title', '')}\n"
                f"Location: {location}\n"
                f"Source: Greenhouse ATS"
            )

            results.append(
                RawLead(
                    source=f"{self.source_name}_greenhouse",
                    raw_text=raw_text,
                    url=job_url,
                    brand_name_hint=slug.replace("-", " ").title(),
                )
            )

        return results

    def _fetch_lever(self, slug: str) -> list[RawLead]:
        """
        Query a company's public Lever job board API for creator roles.
        URL pattern: https://api.lever.co/v0/postings/{slug}?mode=json
        """
        url = f"https://api.lever.co/v0/postings/{slug}?mode=json"
        response = self.session.get(url, timeout=SCRAPER_TIMEOUT)

        if response.status_code == 404:
            logger.debug(f"Lever board '{slug}' not found (404)")
            return []

        response.raise_for_status()
        postings = response.json()
        results: list[RawLead] = []

        if not isinstance(postings, list):
            return []

        for posting in postings:
            title = posting.get("text", "").lower()

            # Filter for creator/influencer marketing roles only
            if not any(kw in title for kw in CREATOR_JOB_KEYWORDS):
                continue

            categories = posting.get("categories", {})
            location = categories.get("location", "Remote")
            team = categories.get("team", "")
            posting_url = posting.get("hostedUrl", url)

            raw_text = (
                f"Company: {slug.replace('-', ' ').title()}\n"
                f"Job Title: {posting.get('text', '')}\n"
                f"Team: {team}\n"
                f"Location: {location}\n"
                f"Source: Lever ATS"
            )

            results.append(
                RawLead(
                    source=f"{self.source_name}_lever",
                    raw_text=raw_text,
                    url=posting_url,
                    brand_name_hint=slug.replace("-", " ").title(),
                )
            )

        return results
