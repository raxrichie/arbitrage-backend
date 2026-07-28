import asyncio
import logging
import httpx
from typing import Dict, List, Any, Optional
from playwright.async_api import async_playwright

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("scrapers")

REAL_BROWSER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
}


def safe_float(val: Any, default: float = 1.0) -> float:
    """Safely parses raw odds to float values (> 1.01)."""
    try:
        if val is None:
            return default
        parsed = float(val)
        return parsed if parsed > 1.01 else default
    except (ValueError, TypeError):
        return default


async def fetch_api(client: httpx.AsyncClient, url: str, bookmaker: str, extra_headers: Optional[dict] = None) -> List[Dict[str, Any]]:
    headers = {**REAL_BROWSER_HEADERS, **(extra_headers or {})}
    try:
        response = await client.get(url, headers=headers, timeout=10.0, follow_redirects=True)
        if response.status_code == 200:
            try:
                data = response.json()
                return parse_raw_data(bookmaker, data)
            except Exception:
                logger.warning(f"[{bookmaker}] Non-JSON payload received")
        else:
            logger.warning(f"[{bookmaker}] HTTP Status {response.status_code}")
    except Exception as e:
        logger.error(f"[{bookmaker}] HTTPX Fetch Error: {e}")
    return []


async def fetch_with_playwright(url: str, bookmaker: str) -> List[Dict[str, Any]]:
    """Playwright worker fallback for strict WAF protection."""
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            context = await browser.new_context(user_agent=REAL_BROWSER_HEADERS["User-Agent"])
            page = await context.new_page()
            
            response = await page.goto(url, wait_until="networkidle", timeout=15000)
            if response and response.status == 200:
                try:
                    data = await response.json()
                    await browser.close()
                    return parse_raw_data(bookmaker, data)
                except Exception:
                    pass
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
        # --- 1. BetPawa (Microservice Format) ---
        if bookmaker == "betpawa":
            events = data.get("events", []) if isinstance(data, dict) else []
            for item in events:
                name = item.get("name", "")
                parts = name.split(" vs ") if " vs " in name else name.split(" - ")
                home = parts[0].strip() if len(parts) == 2 else "Home"
                away = parts[1].strip() if len(parts) == 2 else "Away"

                o1, oX, o2 = 1.0, 1.0, 1.0
                for market in item.get("markets", []):
                    if market.get("name") in ["1X2", "3-Way"]:
                        for outcome in market.get("outcomes", []):
                            oname = outcome.get("name")
                            if oname == "1": o1 = safe_float(outcome.get("odds"))
                            elif oname == "X": oX = safe_float(outcome.get("odds"))
                            elif oname == "2": o2 = safe_float(outcome.get("odds"))

                matches.append({
                    "bookmaker": bookmaker,
                    "id": str(item.get("id") or ""),
                    "homeTeam": home,
                    "awayTeam": away,
                    "league": "Soccer",
                    "startTime": str(item.get("startTime") or ""),
                    "odds": {"1": o1, "X": oX, "2": o2}
                })
            return matches

        # --- 2. Mostbet (Coefficients Object Format) ---
        if bookmaker == "mostbet":
            events = data.get("events", []) if isinstance(data, dict) else []
            for item in events:
                coeffs = item.get("coefficients", {})
                matches.append({
                    "bookmaker": bookmaker,
                    "id": str(item.get("id") or ""),
                    "homeTeam": str(item.get("home") or item.get("homeTeam") or "Home"),
                    "awayTeam": str(item.get("away") or item.get("awayTeam") or "Away"),
                    "league": "Soccer",
                    "startTime": str(item.get("startTime") or ""),
                    "odds": {
                        "1": safe_float(coeffs.get("1")),
                        "X": safe_float(coeffs.get("X")),
                        "2": safe_float(coeffs.get("2"))
                    }
                })
            return matches

        # --- 3. 1Win (Data Array + Outcomes Rates Schema) ---
        if bookmaker == "1win":
            events = data.get("data", []) if isinstance(data, dict) else []
            for item in events:
                o1, oX, o2 = 1.0, 1.0, 1.0
                for outcome in item.get("outcomes", []):
                    t = str(outcome.get("type"))
                    if t == "1": o1 = safe_float(outcome.get("rate"))
                    elif t in ["X", "2"]: oX = safe_float(outcome.get("rate")) if t == "X" else oX
                    if t == "2": o2 = safe_float(outcome.get("rate"))

                matches.append({
                    "bookmaker": bookmaker,
                    "id": str(item.get("id") or ""),
                    "homeTeam": str(item.get("team1") or "Home"),
                    "awayTeam": str(item.get("team2") or "Away"),
                    "league": "Soccer",
                    "startTime": str(item.get("startTime") or ""),
                    "odds": {"1": o1, "X": oX, "2": o2}
                })
            return matches

        # --- 4. 888bet (Nested Odds Map Format) ---
        if bookmaker == "888bet":
            events = data.get("events", []) if isinstance(data, dict) else []
            for item in events:
                name = item.get("name", "")
                parts = name.split(" vs ") if " vs " in name else name.split(" - ")
                home = parts[0].strip() if len(parts) == 2 else "Home"
                away = parts[1].strip() if len(parts) == 2 else "Away"

                raw_odds = item.get("odds", {})
                matches.append({
                    "bookmaker": bookmaker,
                    "id": str(item.get("eventId") or item.get("id") or ""),
                    "homeTeam": home,
                    "awayTeam": away,
                    "league": "Soccer",
                    "startTime": str(item.get("startTime") or ""),
                    "odds": {
                        "1": safe_float(raw_odds.get("home")),
                        "X": safe_float(raw_odds.get("draw")),
                        "2": safe_float(raw_odds.get("away"))
                    }
                })
            return matches

        # --- 5. 1xBet / 22Bet / Helabet Schema ---
        if bookmaker in ["1xbet", "22bet", "helabet"]:
            events = data.get("Value", []) if isinstance(data, dict) else []
            for item in events:
                if isinstance(item, dict):
                    home = item.get("O1") or item.get("HT")
                    away = item.get("O2") or item.get("AT")
                    o1, oX, o2 = 1.0, 1.0, 1.0
                    for outcome in item.get("E", []):
                        t = outcome.get("T")
                        if t == 1: o1 = safe_float(outcome.get("C"))
                        elif t == 2: oX = safe_float(outcome.get("C"))
                        elif t == 3: o2 = safe_float(outcome.get("C"))

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

        # --- 6. Generic Default Schema (Betika, SportyBet, PremierBet, KingBet, etc.) ---
        events = []
        if isinstance(data, list):
            events = data
        elif isinstance(data, dict):
            events = data.get("data") or data.get("events") or data.get("matches") or data.get("items") or data.get("elements") or [data]

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
# INDIVIDUAL SCRAPER ROUTINES (ALL 21 SPORTSBOOKS)
# -------------------------------------------------------------------

async def get_betpawa(client: httpx.AsyncClient):
    url = "https://pawa-offering-service.betpawa.co.tz/offering/v1/events?sportId=1&limit=100"
    res = await fetch_api(client, url, "betpawa")
    return res if len(res) > 0 else await fetch_api(client, "https://www.betpawa.co.tz/api/offering/v1/events", "betpawa")

async def get_mostbet(client: httpx.AsyncClient):
    url = "https://mostbet.com/api/v1/line?sportId=1"
    res = await fetch_api(client, url, "mostbet")
    return res if len(res) > 0 else await fetch_with_playwright("https://mostbet.co.tz", "mostbet")

async def get_1win(client: httpx.AsyncClient):
    url = "https://1win.pro/api/v1/sports/1/events?limit=100"
    res = await fetch_api(client, url, "1win")
    return res if len(res) > 0 else await fetch_with_playwright("https://1win.pro", "1win")

async def get_888bet(client: httpx.AsyncClient):
    url = "https://sports-api.888bet.co.tz/v1/sports/1/events"
    extra_headers = {"Origin": "https://888bet.co.tz"}
    res = await fetch_api(client, url, "888bet", extra_headers=extra_headers)
    return res if len(res) > 0 else await fetch_api(client, "https://sb2frontend-api.888bet.co.tz/api/Sports/GetEvents?sportId=1", "888bet")

async def get_kingbet(client: httpx.AsyncClient):
    url = "https://kingbet.co.tz/api/v1/sports/events?sport=football"
    return await fetch_api(client, url, "kingbet")

async def get_betika(client: httpx.AsyncClient):
    return await fetch_api(client, "https://api.betika.com/v1/uo/matches?limit=100&sub_type=prematch", "betika")

async def get_sportybet(client: httpx.AsyncClient):
    return await fetch_api(client, "https://www.sportybet.com/api/tz/factsCenter/pcUpcomingEvents?sportId=sr%3Asport%3A1&marketId=1%2C18%2C10%2C29%2C11%2C26%2C36%2C14%2C60100&pageSize=100&pageNum=1&option=1", "sportybet")

async def get_sportpesa(client: httpx.AsyncClient):
    return await fetch_api(client, "https://www.sportpesa.co.tz/api/games/highlights?sportId=1", "sportpesa")

async def get_premierbet(client: httpx.AsyncClient):
    return await fetch_api(client, "https://sports-api.premierbet.co.tz/v1/events/highlights?country=TZ&group=g2&platform=desktop&locale=sw&sportId=1&limit=10", "premierbet")

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

async def get_parimatch(client: httpx.AsyncClient):
    url = "https://parimatch.co.tz/api/v1/sports/1/prematch"
    res = await fetch_api(client, url, "parimatch")
    return res if len(res) > 0 else await fetch_with_playwright(url, "parimatch")

async def get_betway(client: httpx.AsyncClient):
    return await fetch_api(client, "https://www.betway.co.tz/sportsapi/br/v1/BetBook/Highlights/?countryCode=TZ&sportId=soccer&Skip=0&Take=20&cultureCode=sw-TZ&isEsport=false&boostedOnly=false&marketTypes=[Win/Draw/Win]", "betway")

async def get_sokabet(client: httpx.AsyncClient):
    return await fetch_api(client, "https://sb2frontend-altenar2.biahosted.com/api/Sportsbook/GetTopEvents?culture=en-GB&integration=sokabet", "sokabet")

async def get_gsb(client: httpx.AsyncClient):
    url = "https://gsb.co.tz/services/evapi/event/GetSportsTree?statusId=0&eventTypeId=0"
    res = await fetch_api(client, url, "gsb")
    return res if len(res) > 0 else await fetch_with_playwright(url, "gsb")

async def get_wasafibet(client: httpx.AsyncClient):
    url = "https://api.wasafibet.com/wb/sportsbook?sport_id=soccer&src=1&producer=3&type=matches&platform=desktop"
    res = await fetch_api(client, url, "wasafibet")
    return res if len(res) > 0 else await fetch_with_playwright(url, "wasafibet")

async def get_bangbet(client: httpx.AsyncClient):
    return await fetch_api(client, "https://bet-api.bangbet.com/api/bet/match/listTop?country=tz", "bangbet")

async def get_leonbet(client: httpx.AsyncClient):
    return await fetch_api(client, "https://leonbet.co.tz/api-2/betline/events/all?ctag=en-US", "leonbet")

async def get_thronebet(client: httpx.AsyncClient):
    return await fetch_api(client, "https://api.thronebet.com/api/v2/multi", "thronebet")


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
    async with httpx.AsyncClient(timeout=12.0) as client:
        tasks = [func(client) for func in BOOKMAKER_MAP.values()]
        results = await asyncio.gather(*tasks, return_exceptions=True)

    all_matches = []
    for res in results:
        if isinstance(res, list):
            all_matches.extend(res)
    return all_matches
