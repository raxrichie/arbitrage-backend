import asyncio
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Dict, Any, List, Optional

from fastapi import FastAPI, APIRouter, Query, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware

from app.scrapers import scrape_all_sportsbooks, BOOKMAKER_MAP

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("main")

# Mutex lock to prevent overlapping Playwright / HTTP scans
SCAN_LOCK = asyncio.Lock()

# In-Memory Cache Engine
CACHE: Dict[str, Any] = {
    "last_updated": None,
    "matches": [],
    "arbitrage_opportunities": [],
    "by_bookmaker": {bm: [] for bm in BOOKMAKER_MAP.keys()},
}


# -------------------------------------------------------------------
# BACKGROUND SCAN WORKER (LOCKED & ATOMIC)
# -------------------------------------------------------------------

async def background_radar_scan():
    """Background task with mutex locking and atomic cache updates."""
    if SCAN_LOCK.locked():
        logger.info("Scan request received, but a radar scan is already running. Skipping duplicate task.")
        return

    async with SCAN_LOCK:
        logger.info("Starting scheduled odds scan across all sportsbooks...")
        try:
            # Returns dict: {"matches": [...], "arbitrage_opportunities": [...], ...}
            scan_data = await scrape_all_sportsbooks()

            # Extract list structures safely
            all_matches = scan_data.get("matches", []) if isinstance(scan_data, dict) else (scan_data if isinstance(scan_data, list) else [])
            arb_ops = scan_data.get("arbitrage_opportunities", []) if isinstance(scan_data, dict) else []

            # Populate bookmaker cache buckets
            by_bm = {bm: [] for bm in BOOKMAKER_MAP.keys()}
            for m in all_matches:
                if isinstance(m, dict):
                    bm_key = str(m.get("bookmaker_id") or m.get("bookmaker") or "").lower().strip()
                    if bm_key in by_bm:
                        by_bm[bm_key].append(m)

            # Atomic Cache Swap (Prevents race conditions mid-read)
            new_cache = {
                "matches": all_matches,
                "arbitrage_opportunities": arb_ops,
                "by_bookmaker": by_bm,
                "last_updated": datetime.now(timezone.utc).isoformat(),
            }
            CACHE.update(new_cache)

            active_count = len([bm for bm, lst in by_bm.items() if len(lst) > 0])
            logger.info(
                f"Scan complete. Updated cache with {len(all_matches)} total matches and "
                f"{len(arb_ops)} surebets across {active_count} active sportsbooks."
            )
        except Exception as e:
            logger.error(f"Error during background scan: {repr(e)}")


# -------------------------------------------------------------------
# FASTAPI LIFESPAN HANDLER (MODERN FASTAPI STANDARD)
# -------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Initializing Arbitrage Radar Backend Service...")
    asyncio.create_task(background_radar_scan())
    logger.info("Initial background scan worker spawned.")
    yield
    logger.info("Shutting down Arbitrage Radar Backend Service...")


app = FastAPI(
    title="Arbitrage Radar Backend API",
    description="Middleware service providing aggregated odds and arbitrage opportunities across Tanzanian bookmakers.",
    version="1.0.0",
    lifespan=lifespan,
)

# Enable CORS for DaxRadar Android App / Frontend clients
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# -------------------------------------------------------------------
# ROUTERS AND ENDPOINTS
# -------------------------------------------------------------------

@app.get("/")
@app.head("/")
async def root():
    return {
        "status": "live",
        "service": "Arbitrage Radar Backend",
        "is_scanning": SCAN_LOCK.locked(),
        "timestamp": CACHE["last_updated"],
        "total_cached_events": len(CACHE["matches"]),
        "total_surebets_found": len(CACHE["arbitrage_opportunities"]),
    }


v1_router = APIRouter(prefix="/v1")


@v1_router.get("/arbitrage-radar")
async def get_arbitrage_radar(background_tasks: BackgroundTasks):
    """Returns global metrics, per-site event counts, and live surebets."""
    counts_by_site = {bm: len(lst) for bm, lst in CACHE["by_bookmaker"].items()}

    # Safely trigger background refresh if cache is cold or lock is free
    if not SCAN_LOCK.locked():
        background_tasks.add_task(background_radar_scan)

    return {
        "status": "success",
        "timestamp": CACHE["last_updated"],
        "is_scanning": SCAN_LOCK.locked(),
        "total_events": len(CACHE["matches"]),
        "total_surebets": len(CACHE["arbitrage_opportunities"]),
        "counts_by_bookmaker": counts_by_site,
        "arbitrage_opportunities": CACHE["arbitrage_opportunities"],
        "opportunities": CACHE["matches"][:100],  # Return top 100 preview
    }


@v1_router.get("/surebets")
@v1_router.get("/arbitrage-opportunities")
async def get_surebets():
    """Returns cross-bookmaker surebet opportunities with calculated stakes."""
    return {
        "status": "success",
        "timestamp": CACHE["last_updated"],
        "count": len(CACHE["arbitrage_opportunities"]),
        "arbitrage_opportunities": CACHE["arbitrage_opportunities"],
    }


@v1_router.get("/odds")
@v1_router.get("/matches")
async def get_bookmaker_odds(
    bookmaker: Optional[str] = Query(None),
    offset: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=500)
):
    """Allows filtering odds by bookmaker ID with offset/limit pagination."""
    if not bookmaker:
        all_m = CACHE["matches"]
        paginated = all_m[offset : offset + limit]
        return {
            "status": "success",
            "total_count": len(all_m),
            "offset": offset,
            "limit": limit,
            "count": len(paginated),
            "matches": paginated,
        }

    bm_key = bookmaker.lower().strip()
    bm_matches = CACHE["by_bookmaker"].get(bm_key, [])
    paginated = bm_matches[offset : offset + limit]

    return {
        "status": "success",
        "bookmaker": bm_key,
        "total_count": len(bm_matches),
        "offset": offset,
        "limit": limit,
        "count": len(paginated),
        "matches": paginated,
    }


@v1_router.post("/trigger-scan")
async def trigger_manual_scan(background_tasks: BackgroundTasks):
    """Allows manual trigger for an immediate refresh scan."""
    if SCAN_LOCK.locked():
        return {
            "status": "busy",
            "message": "Scan already in progress. Task ignored to prevent resource contention."
        }

    background_tasks.add_task(background_radar_scan)
    return {"status": "accepted", "message": "Background refresh scan initiated."}


# Mount API V1 routes
app.include_router(v1_router)
