import asyncio
import logging
import httpx
from typing import Dict, List, Any, Optional
from playwright.async_api import async_playwright

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("scrapers")

# Real Chrome Browser Headers to bypass standard WAF filters
REAL_BROWSER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "sec-ch-ua": '"Not/A)Brand";v="8", "Chromium";v="126", "Google Chrome";v="126"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-origin",
}


def safe_float(val: Any, default: float = 1.0) -> float:
    """Converts raw values to clean floating-point odds."""
    try:
        if val is None:
            return default
        parsed = float(val)
        return parsed if parsed > 1.01 else default
    except (ValueError, TypeError):
        return default


async def fetch_api(client: httpx.AsyncClient, url: str, bookmaker: str, extra_headers: Optional[dict] = None) -> List[Dict[str, Any]]:
    """Fast async HTTP client runner."""
    headers = {**REAL_BROWSER_HEADERS, **(extra_headers or {})}
    try:
        response = await client.get(url, headers=headers, timeout=10.0)
        if response.status_code == 200:
            return parse_raw_data(bookmaker, response.json())
        logger.warning(f"[{bookmaker}] HTTP Status {response.status_code}")
    except Exception as e:
        logger.error(f"[{bookmaker}] HTTPX Fetch Error: {e}")
    return []


async def fetch_with_playwright(url: str, bookmaker: str) -> List[Dict[str, Any]]:
    """Headless browser worker for Cloudflare-protected sites (1xBet, 22Bet, Meridianbet, etc.)."""
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            context = await browser.new_context(user_agent=REAL_BROWSER_HEADERS["User-Agent"])
            page = await context.new_page()
            
            response = await page.goto(url, wait_until="networkidle", timeout=15000)
            if response and response.status == 200:
                data = await response.json()
                await browser.close()
                return parse_raw_data(bookmaker, data)
            await browser.close()
    except Exception as e:
        logger.error(f"[{bookmaker}] Playwright Worker Error: {e}")
    return []


# -------------------------------------------------------------------
# SITE-SPECIFIC PARSER ENGINE
# -------------------------------------------------------------------

def parse_raw_data(bookmaker: str, data: Any) -> List[Dict[str, Any]]:
    matches = []
    try:
        # --- 1. 1xBet / 22Bet / Helabet Schema ---
        if bookmaker in ["1xbet", "22bet", "helabet"]:
            events = data.get("Value", []) if isinstance(data, dict) else []
            for item in events:
                home = item.get("O1") or item.get("HT")
                away = item.get("O2") or item.get("AT")
                
                # Extract 1X2 market odds from 1xBet "E" array
                o1, oX, o2 = 1.0, 1.0, 1.0
                for outcome in item.get("E", []):
                    t = outcome.get("T")
                    if t == 1:
                        o1 = safe_float(outcome.get("C"))
                    elif t == 2:
                        oX = safe_float(outcome.get("C"))
                    elif t == 3:
                        o2 = safe_float(outcome.get("C"))

                if home and away:
                    matches.append({
                        "bookmaker": bookmaker,
                        "id": str(item.get("I") or ""),
                        "homeTeam": str(home),
                        "awayTeam": str(away),
                        "league": str(item.get("LE") or "Soccer"),
                        "startTime": str(item.get("S") or ""),
                        "odds": {"1": o1, "X": oX, "2": o2}
                    })
            return matches

        # --- 2. Meridianbet Schema ---
        if bookmaker == "meridianbet":
            events = data if isinstance(data, list) else data.get("elements", [])
            for item in events:
                home = item.get("homeTeamName") or item.get("home")
                away = item.get("awayTeamName") or item.get("away")
                
                o1, oX, o2 = 1.0, 1.0, 1.0
                for position in item.get("positions", []):
                    code = position.get("code")
                    if code == "1": o1 = safe_float(position.get("value"))
                    elif code == "X": oX = safe_float(position.get("value"))
                    elif code == "2": o2 = safe_float(position.get("value"))

                if home and away:
                    matches.append({
                        "bookmaker": bookmaker,
                        "id": str(item.get("id") or ""),
                        "homeTeam": str(home),
                        "awayTeam": str(away),
                        "league": str(item.get("leagueName") or "Soccer"),
                        "startTime": str(item.get("startTime") or ""),
                        "odds": {"1": o1, "X": oX, "2": o2}
                    })
            return matches

        # --- 3. Generic & Standard JSON Schema (Betika, SportyBet, BetPawa, etc.) ---
        events = []
        if isinstance(data, dict):
            events = data.get("data") or data.get("events") or data.get("matches") or data.get("items") or [data]
        elif isinstance(data, list):
            events = data

        for item in events:
            if isinstance(item, dict):
                home = item.get("homeTeam") or item.get("home_team") or item.get("homeName") or item.get("team1")
                away = item.get("awayTeam") or item.get("away_team") or item.get("awayName") or item.get("team2")
                
                raw_odds = item.get("odds") or {}
                o1 = safe_float(item.get("odds1") or item.get("homeOdds") or raw_odds.get("1"))
                oX = safe_float(item.get("oddsX") or item.get("drawOdds") or raw_odds.get("X"))
                o2 = safe_float(item.get("odds2") or item.get("awayOdds") or raw_odds.get("2"))

                if home and away:
                    matches.append({
                        "bookmaker": bookmaker,
                        "id": str(item.get("id") or item.get("eventId") or item.get("gameId") or ""),
                        "homeTeam": str(home),
                        "awayTeam": str(away),
                        "league": str(item.get("league") or item.get("categoryName") or "Soccer"),
                        "startTime": str(item.get("startTime") or item.get("eventDate") or ""),
                        "odds": {"1": o1, "X": oX, "2": o2}
                    })

    except Exception as e:
        logger.error(f"[{bookmaker}] Parser Exception: {e}")

    return matches


# -------------------------------------------------------------------
# INDIVIDUAL SCRAPERS (1 - 21)
# -------------------------------------------------------------------

# Working Sites (Untouched API logic)
async def get_betika(client: httpx.AsyncClient):
    return await fetch_api(client, "https://api.betika.com/v1/uo/matches?limit=100&sub_type=prematch", "betika")

async def get_sportybet(client: httpx.AsyncClient):
    return await fetch_api(client, "https://www.sportybet.com/api/tz/factsCenter/pcUpcomingEvents?sportId=sr%3Asport%3A1&marketId=1%2C18%2C10%2C29%2C11%2C26%2C36%2C14%2C60100&pageSize=100&pageNum=1&option=1", "sportybet")

async def get_betpawa(client: httpx.AsyncClient):
    return await fetch_api(client, "https://sports-api.betpawa.co.tz/v1/sports/1/events?categoryId=2", "betpawa")

async def get_sportpesa(client: httpx.AsyncClient):
    return await fetch_api(client, "https://www.sportpesa.co.tz/api/games/highlights?sportId=1", "sportpesa")

async def get_premierbet(client: httpx.AsyncClient):
    return await fetch_api(client, "https://sports-api.premierbet.co.tz/v1/events/highlights?country=TZ&group=g2&platform=desktop&locale=sw&sportId=1&limit=10", "premierbet")


# Protected/Custom Schema Sites (HTTPX -> Playwright Fallback)
async def get_1xbet(client: httpx.AsyncClient):
    url = "https://1xbet.tz/service-api/main-line-feed/v1/expressDay?cfView=3&country=181&gr=1499&lng=en&ref=398"
    res = await fetch_api(client, url, "1xbet")
    return res if len(res) > 0 else await fetch_with_playwright(url, "1xbet")

async def get_22bet(client: httpx.AsyncClient):
    url = "https://22bet.co.tz/service-api/main-line-feed/v1/expressDay?cfView=3&country=181&gr=1499&lng=en&ref=398"
    res = await fetch_api(client, url, "22bet")
    return res if len(res) > 0 else await fetch_with_playwright(url, "22bet")

async def get_helabet(client: httpx.AsyncClient):
    url = "https://helabet.co.tz/service-api/main-line-feed/v1/expressDay?cfView=3&country=181&gr=772&lng=en&ref=237"
    res = await fetch_api(client, url, "helabet")
    return res if len(res) > 0 else await fetch_with_playwright(url, "helabet")

async def get_meridianbet(client: httpx.AsyncClient):
    url = "https://meridianbet.co.tz/api/v1/events/highlights"
    res = await fetch_api(client, url, "meridianbet")
    return res if len(res) > 0 else await fetch_with_playwright(url, "meridianbet")

async def get_mostbet(client: httpx.AsyncClient):
    url = "https://mostbet-tz3.com/api/v3/user/line/events.json?ss=all&l=2&ltr=0"
    res = await fetch_api(client, url, "mostbet")
    return res if len(res) > 0 else await fetch_with_playwright(url, "mostbet")

async def get_parimatch(client: httpx.AsyncClient):
    url = "https://parimatch.co.tz/api/v1/sports/1/prematch"
    res = await fetch_api(client, url, "parimatch")
    return res if len(res) > 0 else await fetch_with_playwright(url, "parimatch")

async def get_betway(client: httpx.AsyncClient):
    url = "https://www.betway.co.tz/sportsapi/br/v1/BetBook/Highlights/?countryCode=TZ&sportId=soccer&Skip=0&Take=20&cultureCode=sw-TZ&isEsport=false&boostedOnly=false&marketTypes=[Win/Draw/Win]"
    res = await fetch_api(client, url, "betway")
    return res if len(res) > 0 else await fetch_with_playwright(url, "betway")


# Direct API Endpoints with Real Headers
async def get_1win(client: httpx.AsyncClient):
    return await fetch_api(client, "https://api-gateway.top-parser.com/matches/get-many", "1win")

async def get_bangbet(client: httpx.AsyncClient):
    return await fetch_api(client, "https://bet-api.bangbet.com/api/bet/match/listTop?country=tz", "bangbet")

async def get_888bet(client: httpx.AsyncClient):
    return await fetch_api(client, "https://888bet.tz/api-v2/league-card/d/2/tz888/867918-868453-868821-867871-869014-869016-854030-854047-856943-853975/eyJyZXF1ZXN0Qm9keSI6eyJzZWFzd24Ijo1ODY3OTE4LDg2ODgzMSw4Njg4MjEsODY3ODcxLDg2OTAxNCw4NjkwMTYsODU0MDMwLDg1NDA0Nyw4NTY5NDMsODUzOTc1XX19=", "888bet")

async def get_sokabet(client: httpx.AsyncClient):
    return await fetch_api(client, "https://sb2frontend-altenar2.biahosted.com/api/Sportsbook/GetTopEvents?culture=en-GB&integration=sokabet", "sokabet")

async def get_gsb(client: httpx.AsyncClient):
    return await fetch_api(client, "https://gsb.co.tz/services/evapi/event/GetSportsTree?statusId=0&eventTypeId=0", "gsb")

async def get_leonbet(client: httpx.AsyncClient):
    return await fetch_api(client, "https://leonbet.co.tz/api-2/betline/events/all?ctag=en-US", "leonbet")

async def get_kingbet(client: httpx.AsyncClient):
    return await fetch_api(client, "https://www.kingbet.co.tz/api/redis_data/home", "kingbet")

async def get_thronebet(client: httpx.AsyncClient):
    return await fetch_api(client, "https://api.thronebet.com/api/v2/multi", "thronebet")

async def get_wasafibet(client: httpx.AsyncClient):
    return await fetch_api(client, "https://api.wasafibet.com/wb/sportsbook?sport_id=soccer&src=1&producer=3&type=matches&profile_id=&msisdn=&bm_leagues=&resource=sport&attribution=copilot.com&platform=desktop", "wasafibet")


# -------------------------------------------------------------------
# REGISTRY MAP & CONCURRENT RUNNER
# -------------------------------------------------------------------

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
    """Runs all 21 scrapers concurrently."""
    async with httpx.AsyncClient(timeout=12.0) as client:
        tasks = [func(client) for func in BOOKMAKER_MAP.values()]
        results = await asyncio.gather(*tasks, return_exceptions=True)

    all_matches = []
    for res in results:
        if isinstance(res, list):
            all_matches.extend(res)
    return all_matches
