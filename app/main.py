import asyncio
import time
from fastapi import FastAPI, Query, APIRouter
from fastapi.middleware.cors import CORSMiddleware
import httpx

from app.scrapers import scrape_all_sportsbooks, BOOKMAKER_MAP

app = FastAPI(title="DaxRadar Central Middleware API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- IN-MEMORY CACHE ---
CACHE = {
    "matches": [],
    "by_bookmaker": {},
    "last_updated": 0
}

# --- BACKGROUND SCRAPER LOOP ---
async def background_scraper_loop():
    """Runs continuously in the background, updating match cache every 20 seconds."""
    while True:
        try:
            matches = await scrape_all_sportsbooks()
            
            # Organize by bookmaker for quick fallback lookup
            by_bm = {}
            for m in matches:
                bm = m.get("bookmaker")
                if bm not in by_bm:
                    by_bm[bm] = []
                by_bm[bm].append(m)

            # Atomic cache update
            CACHE["matches"] = matches
            CACHE["by_bookmaker"] = by_bm
            CACHE["last_updated"] = int(time.time())
            
        except Exception as e:
            print(f"[CACHE WORKER ERROR] {e}")
            
        await asyncio.sleep(20)  # Refresh interval in seconds

@app.on_event("startup")
async def start_background_tasks():
    asyncio.create_task(background_scraper_loop())

# --- ROUTER DEFINITIONS ---
v1_router = APIRouter(prefix="/v1")

@app.get("/")
@v1_router.get("/")
def read_root():
    return {
        "status": "online",
        "service": "DaxRadar Central Scraper Server",
        "cached_matches": len(CACHE["matches"]),
        "last_updated": CACHE["last_updated"]
    }

@v1_router.get("/arbitrage-radar")
async def get_arbitrage_radar():
    """Serves instant cached data in <20ms."""
    return {
        "status": "success",
        "timestamp": CACHE["last_updated"],
        "total_opportunities": len(CACHE["matches"]),
        "opportunities": CACHE["matches"]
    }

@v1_router.get("/odds")
@v1_router.get("/matches")
async def get_bookmaker_odds(bookmaker: str = Query(None)):
    if not bookmaker:
        return {"status": "success", "count": len(CACHE["matches"]), "matches": CACHE["matches"]}
        
    key = bookmaker.lower()
    matches = CACHE["by_bookmaker"].get(key, [])
    return {"status": "success", "bookmaker": key, "matches": matches}

@v1_router.get("/sportsbooks/{bookmaker}")
@v1_router.get("/{bookmaker}/matches")
async def get_specific_bookmaker(bookmaker: str):
    key = bookmaker.lower()
    matches = CACHE["by_bookmaker"].get(key, [])
    return {"status": "success", "bookmaker": key, "matches": matches}

app.include_router(v1_router)
