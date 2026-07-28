import asyncio
import logging
from datetime import datetime, timezone
from typing import Dict, Any, List

from fastapi import FastAPI, APIRouter, Query, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware

from app.scrapers import scrape_all_sportsbooks, BOOKMAKER_MAP

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("main")

app = FastAPI(
    title="Arbitrage Radar Backend API",
    description="Middleware service providing aggregated odds and arbitrage opportunities across Tanzanian bookmakers.",
    version="1.0.0",
)

# Enable CORS for DaxRadar Android App / Frontend clients
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# In-Memory Cache Engine
CACHE: Dict[str, Any] = {
    "last_updated": None,
    "matches": [],
    "by_bookmaker": {bm: [] for bm in BOOKMAKER_MAP.keys()},
}

# -------------------------------------------------------------------
# BACKGROUND SCAN WORKER
# -------------------------------------------------------------------

async def background_radar_scan():
    """Background task to fetch live odds across all sportsbooks and update cache."""
    logger.info("Starting scheduled odds scan across all sportsbooks...")
    try:
        all_matches = await scrape_all_sportsbooks()
        
        # Reset and populate bookmaker cache buckets
        by_bm = {bm: [] for bm in BOOKMAKER_MAP.keys()}
        for m in all_matches:
            bm_key = m.get("bookmaker", "").lower()
            if bm_key in by_bm:
                by_bm[bm_key].append(m)

        CACHE["matches"] = all_matches
        CACHE["by_bookmaker"] = by_bm
        CACHE["last_updated"] = datetime.now(timezone.utc).isoformat()

        logger.info(
            f"Scan complete. Updated cache with {len(all_matches)} total matches across "
            f"{len([bm for bm, lst in by_bm.items() if len(lst) > 0])} active sportsbooks."
        )
    except Exception as e:
        logger.error(f"Error during background scan: {e}")


# Start initial scan on startup and schedule periodic updates
@app.on_event("startup")
async def on_startup():
    asyncio.create_task(background_radar_scan())


# -------------------------------------------------------------------
# ROUTERS AND ENDPOINTS
# -------------------------------------------------------------------

# Silences Render Health Check 405 Warnings by adding @app.head("/")
@app.get("/")
@app.head("/")
async def root():
    return {
        "status": "live",
        "service": "Arbitrage Radar Backend",
        "timestamp": CACHE["last_updated"],
        "total_cached_events": len(CACHE["matches"]),
    }


v1_router = APIRouter(prefix="/v1")


@v1_router.get("/arbitrage-radar")
async def get_arbitrage_radar(background_tasks: BackgroundTasks):
    """Returns total global events, individual site event counts (N), and all match opportunities."""
    counts_by_site = {bm: len(lst) for bm, lst in CACHE["by_bookmaker"].items()}

    # Trigger a refresh scan in the background if cache is empty or older than 2 minutes
    background_tasks.add_task(background_radar_scan)

    return {
        "status": "success",
        "timestamp": CACHE["last_updated"],
        "total_events": len(CACHE["matches"]),
        "counts_by_bookmaker": counts_by_site,
        "opportunities": CACHE["matches"],
    }


@v1_router.get("/odds")
@v1_router.get("/matches")
async def get_bookmaker_odds(bookmaker: Optional[str] = Query(None)):
    """Allows filtering odds by specific bookmaker ID or retrieving all matches."""
    if not bookmaker:
        return {
            "status": "success",
            "total_count": len(CACHE["matches"]),
            "matches": CACHE["matches"],
        }

    bm_key = bookmaker.lower().strip()
    matches = CACHE["by_bookmaker"].get(bm_key, [])

    return {
        "status": "success",
        "bookmaker": bm_key,
        "count": len(matches),
        "matches": matches,
    }


@v1_router.post("/trigger-scan")
async def trigger_manual_scan(background_tasks: BackgroundTasks):
    """Allows manual trigger for an immediate refresh scan."""
    background_tasks.add_task(background_radar_scan)
    return {"status": "accepted", "message": "Background refresh scan initiated."}


# Mount API V1 routes
app.include_router(v1_router)
