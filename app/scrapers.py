import asyncio
import logging
import httpx
from typing import Dict, List, Any, Optional

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("scrapers")

DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
}


async def fetch_api(client: httpx.AsyncClient, url: str, bookmaker: str) -> List[Dict[str, Any]]:
    """Helper to fetch and parse JSON endpoints safely via HTTPX."""
    try:
        response = await client.get(url, headers=DEFAULT_HEADERS, timeout=10.0)
        if response.status_code == 200:
            return parse_raw_data(bookmaker, response.json())
        logger.warning(f"[{bookmaker}] Returned HTTP {response.status_code}")
    except Exception as e:
        logger.error(f"[{bookmaker}] HTTP Fetch Error: {e}")
    return []


def parse_raw_data(bookmaker: str, data: Any) -> List[Dict[str, Any]]:
    """Standardizes output format from raw JSON into match objects."""
    matches = []
    try:
        if isinstance(data, dict):
            events = data.get("data") or data.get("events") or data.get("matches") or data.get("items") or [data]
        elif isinstance(data, list):
            events = data
        else:
            events = []

        for item in events:
            if isinstance(item, dict):
                matches.append({
                    "bookmaker": bookmaker,
                    "id": str(item.get("id") or item.get("eventId") or item.get("gameId") or ""),
                    "homeTeam": item.get("homeTeam") or item.get("home_team") or item.get("homeName") or "Home",
                    "awayTeam": item.get("awayTeam") or item.get("away_team") or item.get("awayName") or "Away",
                    "league": item.get("league") or item.get("categoryName") or "Soccer",
                    "startTime": item.get("startTime") or item.get("eventDate") or "",
                    "odds": {
                        "1": item.get("odds1") or item.get("homeOdds") or 1.0,
                        "X": item.get("oddsX") or item.get("drawOdds") or 1.0,
                        "2": item.get("odds2") or item.get("awayOdds") or 1.0,
                    }
                })
    except Exception as e:
        logger.error(f"[{bookmaker}] Parser Error: {e}")
    return matches


# --- INDIVIDUAL BOOKMAKER FETCH FUNCTIONS ---

async def get_betika(client: httpx.AsyncClient):
    return await fetch_api(client, "https://api.betika.com/v1/uo/matches?limit=100&sub_type=prematch", "betika")

async def get_sportybet(client: httpx.AsyncClient):
    return await fetch_api(client, "https://www.sportybet.com/api/tz/factsCenter/pcUpcomingEvents?sportId=sr%3Asport%3A1&marketId=1%2C18%2C10%2C29%2C11%2C26%2C36%2C14%2C60100&pageSize=100&pageNum=1&option=1", "sportybet")

async def get_betpawa(client: httpx.AsyncClient):
    return await fetch_api(client, "https://sports-api.betpawa.co.tz/v1/sports/1/events?categoryId=2", "betpawa")

async def get_meridianbet(client: httpx.AsyncClient):
    return await fetch_api(client, "https://meridianbet.co.tz/api/v1/events/highlights", "meridianbet")

async def get_parimatch(client: httpx.AsyncClient):
    return await fetch_api(client, "https://parimatch.co.tz/api/v1/sports/1/prematch", "parimatch")

async def get_1xbet(client: httpx.AsyncClient):
    return await fetch_api(client, "https://1xbet.tz/service-api/main-line-feed/v1/expressDay?cfView=3&country=181&gr=1499&lng=en&ref=398", "1xbet")

async def get_22bet(client: httpx.AsyncClient):
    return await fetch_api(client, "https://22bet.co.tz/service-api/main-line-feed/v1/expressDay?cfView=3&country=181&gr=1499&lng=en&ref=398", "22bet")

async def get_1win(client: httpx.AsyncClient):
    return await fetch_api(client, "https://api-gateway.top-parser.com/matches/get-many", "1win")

async def get_mostbet(client: httpx.AsyncClient):
    return await fetch_api(client, "https://mostbet-tz3.com/api/v3/user/line/events.json?ss=all&l=2&ltr=0", "mostbet")

async def get_helabet(client: httpx.AsyncClient):
    return await fetch_api(client, "https://helabet.co.tz/service-api/main-line-feed/v1/expressDay?cfView=3&country=181&gr=772&lng=en&ref=237", "helabet")

async def get_bangbet(client: httpx.AsyncClient):
    return await fetch_api(client, "https://bet-api.bangbet.com/api/bet/match/listTop?country=tz", "bangbet")

async def get_888bet(client: httpx.AsyncClient):
    return await fetch_api(client, "https://888bet.tz/api-v2/league-card/d/2/tz888/867918-868453-868821-867871-869014-869016-854030-854047-856943-853975/eyJyZXF1ZXN0Qm9keSI6eyJzZWFzd24Ijo1ODY3OTE4LDg2ODgzMSw4Njg4MjEsODY3ODcxLDg2OTAxNCw4NjkwMTYsODU0MDMwLDg1NDA0Nyw4NTY5NDMsODUzOTc1XX19=", "888bet")

async def get_sokabet(client: httpx.AsyncClient):
    return await fetch_api(client, "https://sb2frontend-altenar2.biahosted.com/api/Sportsbook/GetTopEvents?culture=en-GB&integration=sokabet", "sokabet")

async def get_gsb(client: httpx.AsyncClient):
    return await fetch_api(client, "https://gsb.co.tz/services/evapi/event/GetSportsTree?statusId=0&eventTypeId=0", "gsb")

async def get_premierbet(client: httpx.AsyncClient):
    return await fetch_api(client, "https://sports-api.premierbet.co.tz/v1/events/highlights?country=TZ&group=g2&platform=desktop&locale=sw&sportId=1&limit=10", "premierbet")

async def get_leonbet(client: httpx.AsyncClient):
    return await fetch_api(client, "https://leonbet.co.tz/api-2/betline/events/all?ctag=en-US", "leonbet")

async def get_betway(client: httpx.AsyncClient):
    return await fetch_api(client, "https://www.betway.co.tz/sportsapi/br/v1/BetBook/Highlights/?countryCode=TZ&sportId=soccer&Skip=0&Take=20&cultureCode=sw-TZ&isEsport=false&boostedOnly=false&marketTypes=[Win/Draw/Win]", "betway")

async def get_sportpesa(client: httpx.AsyncClient):
    return await fetch_api(client, "https://www.sportpesa.co.tz/api/games/highlights?sportId=1", "sportpesa")

async def get_kingbet(client: httpx.AsyncClient):
    return await fetch_api(client, "https://www.kingbet.co.tz/api/redis_data/home", "kingbet")

async def get_thronebet(client: httpx.AsyncClient):
    return await fetch_api(client, "https://api.thronebet.com/api/v2/multi", "thronebet")

async def get_wasafibet(client: httpx.AsyncClient):
    return await fetch_api(client, "https://api.wasafibet.com/wb/sportsbook?sport_id=soccer&src=1&producer=3&type=matches&profile_id=&msisdn=&bm_leagues=&resource=sport&attribution=copilot.com&platform=desktop", "wasafibet")


# REGISTRY MAP & CONCURRENT RUNNER (REQUIRED BY MAIN.PY)
BOOKMAKER_MAP = {
    "betika": get_betika,
    "sportybet": get_sportybet,
    "betpawa": get_betpawa,
    "meridianbet": get_meridianbet,
    "parimatch": get_parimatch,
    "1xbet": get_1xbet,
    "22bet": get_22bet,
    "1win": get_1win,
    "mostbet": get_mostbet,
    "helabet": get_helabet,
    "bangbet": get_bangbet,
    "888bet": get_888bet,
    "sokabet": get_sokabet,
    "gsb": get_gsb,
    "premierbet": get_premierbet,
    "leonbet": get_leonbet,
    "betway": get_betway,
    "sportpesa": get_sportpesa,
    "kingbet": get_kingbet,
    "thronebet": get_thronebet,
    "wasafibet": get_wasafibet,
}


async def scrape_all_sportsbooks() -> List[Dict[str, Any]]:
    """Runs concurrent async requests across all 21 sportsbooks."""
    async with httpx.AsyncClient(timeout=12.0) as client:
        tasks = [func(client) for func in BOOKMAKER_MAP.values()]
        results = await asyncio.gather(*tasks, return_exceptions=True)

    all_matches = []
    for res in results:
        if isinstance(res, list):
            all_matches.extend(res)
    return all_matches
