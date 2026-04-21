"""
Closr — Health Check API (FastAPI)
Provides a GET /health endpoint that returns:
  - Live Supabase pool metrics
  - Enrichment budget status
  - Last pipeline run info
  - System timestamp

Run with: uvicorn health:app --host 0.0.0.0 --port 8000
"""

import logging
from datetime import datetime, timezone

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from config import ENVIRONMENT, TIMEZONE
from db.supabase_client import get_pool_stats
from enrichment.enricher import LayeredEnricher

logger = logging.getLogger("closr.health")

app = FastAPI(
    title="Closr Health API",
    description="Lead engine health monitoring and budget status.",
    version="1.0.0",
)


@app.get("/health", response_class=JSONResponse)
async def health_check():
    """
    Return a comprehensive health report including:
    - Pool metrics (current pool size, unenriched count)
    - Last pipeline run details (status, timing, counts)
    - Enrichment budget status per provider
    - System metadata
    """
    try:
        # Fetch pool stats from Supabase
        pool_stats = get_pool_stats()

        # Fetch enrichment budget status
        enricher = LayeredEnricher()
        budget_status = enricher.get_budget_status()

        return JSONResponse(
            status_code=200,
            content={
                "status": "healthy",
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "environment": ENVIRONMENT,
                "timezone": TIMEZONE,
                "pool": {
                    "current_size": pool_stats.get("pool_size", 0),
                    "unenriched_queue": pool_stats.get("unenriched_count", 0),
                },
                "last_pipeline_run": pool_stats.get("last_run"),
                "enrichment_budget": budget_status,
            },
        )
    except Exception as e:
        logger.error(f"Health check failed: {e}")
        return JSONResponse(
            status_code=503,
            content={
                "status": "unhealthy",
                "error": str(e),
                "timestamp": datetime.now(timezone.utc).isoformat(),
            },
        )


@app.get("/")
async def root():
    """Root redirect to health endpoint."""
    return {"message": "Closr Lead Engine", "health": "/health"}
