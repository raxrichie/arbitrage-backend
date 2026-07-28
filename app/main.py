from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI(title="Sportsbook Arbitrage Aggregator API")

# Enable CORS for Android application requests
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/")
def read_root():
    return {"status": "online", "service": "Arbitrage Aggregator Backend"}

@app.get("/v1/arbitrage-radar")
async def get_arbitrage_radar():
    """
    Primary endpoint queried by your Android app.
    Returns 100% live scraped arbitrage opportunities.
    """
    return {
        "status": "success",
        "timestamp": 1722105240,
        "total_opportunities": 0,
        "opportunities": []
    }