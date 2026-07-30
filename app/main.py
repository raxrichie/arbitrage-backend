import asyncio
import logging
from datetime import datetime, timezone
from typing import Dict, Any, List, Optional

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
    "arbitrage_opportunities": [],
    "by_bookmaker": {bm: [] for bm in BOOKMAKER_MAP.keys()},
}

# -------------------------------------------------------------------
# BACKGROUND SCAN WORKER
# -------------------------------------------------------------------

async def background_radar_scan():
    """Background task to fetch live odds across all sportsbooks and update cache."""
    logger.info("Starting scheduled odds scan across all sportsbooks...")
    try:
        # Returns a dict: {"matches": [...], "arbitrage_opportunities": [...], ...}
        scan_data = await scrape_all_sportsbooks()

        # Safely extract list data
        all_matches = scan_data.get("matches", []) if isinstance(scan_data, dict) else (scan_data if isinstance(scan_data, list) else [])
        arb_ops = scan_data.get("arbitrage_opportunities", []) if isinstance(scan_data, dict) else []

        # Reset and populate bookmaker cache buckets
        by_bm = {bm: [] for bm in BOOKMAKER_MAP.keys()}
        
        for m in all_matches:
            if isinstance(m, dict):
                # Look up "bookmaker_id" (matching scrapers.py schema) with fallback to "bookmaker"
                bm_key = str(m.get("bookmaker_id") or m.get("bookmaker") or "").lower().strip()
                if bm_key in by_bm:
                    by_bm[bm_key].append(m)

        CACHE["matches"] = all_matches
        CACHE["arbitrage_opportunities"] = arb_ops
        CACHE["by_bookmaker"] = by_bm
        CACHE["last_updated"] = datetime.now(timezone.utc).isoformat()

        active_count = len([bm for bm, lst in by_bm.items() if len(lst) > 0])
        logger.info(
            f"Scan complete. Updated cache with {len(all_matches)} total matches and "
            f"{len(arb_ops)} surebets across {active_count} active sportsbooks."
        )
    except Exception as e:
        logger.error(f"Error during background scan: {repr(e)}")


# Start initial scan on startup
@app.on_event("startup")
async def on_startup():
    asyncio.create_task(background_radar_scan())


# -------------------------------------------------------------------
# ROUTERS AND ENDPOINTS
# -------------------------------------------------------------------

@app.get("/")
@app.head("/")
async def root():
    return {
        "status": "live",
        "service": "Arbitrage Radar Backend",
        "timestamp": CACHE["last_updated"],
        "total_cached_events": len(CACHE["matches"]),
        "total_surebets_found": len(CACHE["arbitrage_opportunities"]),
    }


v1_router = APIRouter(prefix="/v1")


@v1_router.get("/arbitrage-radar")
async def get_arbitrage_radar(background_tasks: BackgroundTasks):
    """Returns total global events, individual site event counts, and live surebets."""
    counts_by_site = {bm: len(lst) for bm, lst in CACHE["by_bookmaker"].items()}

    # Trigger a refresh scan in the background
    background_tasks.add_task(background_radar_scan)

    return {
        "status": "success",
        "timestamp": CACHE["last_updated"],
        "total_events": len(CACHE["matches"]),
        "total_surebets": len(CACHE["arbitrage_opportunities"]),
        "counts_by_bookmaker": counts_by_site,
        "arbitrage_opportunities": CACHE["arbitrage_opportunities"],
        "opportunities": CACHE["matches"],
    }


@v1_router.get("/surebets")
@v1_router.get("/arbitrage-opportunities")
async def get_surebets():
    """Returns only the high-value cross-bookmaker surebet opportunities with calculated stakes."""
    return {
        "status": "success",
        "timestamp": CACHE["last_updated"],
        "count": len(CACHE["arbitrage_opportunities"]),
        "arbitrage_opportunities": CACHE["arbitrage_opportunities"],
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
