import time
from fastapi import FastAPI, Query, APIRouter
from fastapi.middleware.cors import CORSMiddleware
import httpx

from app.scrapers import scrape_all_sportsbooks, BOOKMAKER_MAP

app = FastAPI(title="DaxRadar Central Middleware API")

# Enable CORS for DaxRadar Android requests
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Router prefix binding for /v1
v1_router = APIRouter(prefix="/v1")

@app.get("/")
@v1_router.get("/")
def read_root():
    return {
        "status": "online",
        "service": "DaxRadar Central Scraper Server",
        "total_sportsbooks": len(BOOKMAKER_MAP)
    }

@v1_router.get("/arbitrage-radar")
async def get_arbitrage_radar():
    """Primary aggregator endpoint for DaxRadar scan cycle."""
    matches = await scrape_all_sportsbooks()
    return {
        "status": "success",
        "timestamp": int(time.time()),
        "total_opportunities": len(matches),
        "opportunities": matches
    }

@v1_router.get("/odds")
@v1_router.get("/matches")
async def get_bookmaker_odds(bookmaker: str = Query(None)):
    """Handles /v1/odds and /v1/matches with query params (?bookmaker=gsb)."""
    if not bookmaker:
        matches = await scrape_all_sportsbooks()
        return {"status": "success", "count": len(matches), "matches": matches}
        
    key = bookmaker.lower()
    if key in BOOKMAKER_MAP:
        async with httpx.AsyncClient(timeout=10.0) as client:
            matches = await BOOKMAKER_MAP[key](client)
        return {"status": "success", "bookmaker": key, "matches": matches}
    return {"status": "error", "message": f"Bookmaker '{bookmaker}' not found", "matches": []}

# Catch dynamic URL routes requested by DaxRadar fallback engine:
# e.g., /v1/sportsbooks/{bookmaker} or /v1/{bookmaker}/matches
@v1_router.get("/sportsbooks/{bookmaker}")
@v1_router.get("/{bookmaker}/matches")
async def get_specific_bookmaker(bookmaker: str):
    key = bookmaker.lower()
    if key in BOOKMAKER_MAP:
        async with httpx.AsyncClient(timeout=10.0) as client:
            matches = await BOOKMAKER_MAP[key](client)
        return {"status": "success", "bookmaker": key, "matches": matches}
    return {"status": "error", "message": f"Bookmaker '{bookmaker}' not found", "matches": []}

# Register /v1 router with FastAPI app
app.include_router(v1_router)
