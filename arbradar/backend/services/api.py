from fastapi import APIRouter

router = APIRouter(prefix="/api", tags=["API"])

# Dummy odds endpoint
@router.get("/odds")
def get_odds():
    return {
        "bookmaker": "SportyBet",
        "event": "Team A vs Team B",
        "market": "Match Winner",
        "odds": {"Team A": 2.10, "Team B": 1.75},
        "timestamp": "2026-07-13T14:55:00"
    }

# Dummy arbitrage endpoint
@router.get("/arbitrage")
def get_arbitrage():
    return {
        "opportunity": True,
        "event": "Team A vs Team B",
        "bookmakers": ["SportyBet", "Betika"],
        "margin": 3.2,
        "stakes": {"Team A": 100, "Team B": 95},
        "profit": 5.0
    }
