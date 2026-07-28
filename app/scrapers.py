import asyncio
import logging
import json
import httpx
from typing import Dict, List, Any, Optional
from playwright.async_api import async_playwright

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("scrapers")

REAL_BROWSER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "sec-ch-ua": '"Not/A)Brand";v="8", "Chromium";v="126", "Google Chrome";v="126"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
}

CUSTOM_TIMEOUT = httpx.Timeout(connect=20.0, read=30.0, write=20.0, pool=20.0)


def safe_float(val: Any, default: float = 1.0) -> float:
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
        response = await client.get(url, headers=headers, timeout=CUSTOM_TIMEOUT)
        if response.status_code == 200:
            try:
                data = response.json()
                matches = parse_raw_data(bookmaker, data)
                
                if len(matches) == 0:
                    if isinstance(data, dict):
                        logger.info(f"[{bookmaker.upper()}] HTTP 200 OK | 0 matches. Top-level Dict Keys: {list(data.keys())[:15]}")
                    elif isinstance(data, list):
                        logger.info(f"[{bookmaker.upper()}] HTTP 200 OK | 0 matches. List Length: {len(data)}")
                else:
                    logger.info(f"[{bookmaker.upper()}] Successfully parsed {len(matches)} matches.")
                
                return matches

            except Exception as parse_err:
                logger.error(f"[{bookmaker}] JSON Decode/Parser Exception: {repr(parse_err)}")
        else:
            logger.warning(f"[{bookmaker}] HTTP Status {response.status_code}")
    except Exception as e:
        logger.error(f"[{bookmaker}] Fetch Error ({type(e).__name__}): {repr(e)}")
    return []


async def fetch_with_playwright(url: str, bookmaker: str) -> List[Dict[str, Any]]:
    """Playwright worker using stable Chromium flags for Docker/Render environments."""
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--disable-dev-shm-usage",
                    "--no-sandbox",
                    "--disable-setuid-sandbox"
                ]
            )
            context = await browser.new_context(user_agent=REAL_BROWSER_HEADERS["User-Agent"])
            page = await context.new_page()
            
            response = await page.goto(url, wait_until="networkidle", timeout=25000)
            if response and response.status == 200:
                try:
                    data = await response.json()
                    await browser.close()
                    matches = parse_raw_data(bookmaker, data)
                    logger.info(f"[{bookmaker.upper()}-PLAYWRIGHT] Successfully parsed {len(matches)} matches.")
                    return matches
                except Exception:
                    pass
            await browser.close()
    except Exception as e:
        logger.error(f"[{bookmaker}] Playwright Error ({type(e).__name__}): {repr(e)}")
    return []


# -------------------------------------------------------------------
# PARSER ENGINE
# -------------------------------------------------------------------

def parse_raw_data(bookmaker: str, data: Any) -> List[Dict[str, Any]]:
    matches = []
    try:
        # --- 1. Betika (Schema Logging Diagnostic) ---
        if bookmaker == "betika":
            events = data.get("data", []) if isinstance(data, dict) else (data if isinstance(data, list) else [])
            if events and isinstance(events, list) and len(events) > 0:
                # LOG THE FIRST EVENT DICTIONARY TO REVEAL KEY NAMES IN TERMINAL LOGS
                logger.info(f"[BETIKA DUMP] First event sample keys/structure: {list(events[0].keys()) if isinstance(events[0], dict) else events[0]}")
            
            for item in events:
                if isinstance(item, dict):
                    home = item.get("home_name") or item.get("homeTeam") or item.get("home") or item.get("home_team_name")
                    away = item.get("away_name") or item.get("awayTeam") or item.get("away") or item.get("away_team_name")
                    
                    o1, oX, o2 = 1.0, 1.0, 1.0
                    raw_odds = item.get("home_odds") or item.get("odds")
                    if isinstance(raw_odds, dict):
                        o1 = safe_float(raw_odds.get("1") or item.get("home_odds"))
                        oX = safe_float(raw_odds.get("X") or item.get("draw_odds"))
                        o2 = safe_float(raw_odds.get("2") or item.get("away_odds"))
                    elif isinstance(raw_odds, list):
                        for market in raw_odds:
                            if isinstance(market, dict):
                                for outcome in market.get("odds", []):
                                    display = str(outcome.get("display") or outcome.get("name") or "")
                                    val = outcome.get("odd_value") or outcome.get("odd")
                                    if display == "1": o1 = safe_float(val)
                                    elif display == "X": oX = safe_float(val)
                                    elif display == "2": o2 = safe_float(val)

                    if home and away:
                        matches.append({
                            "bookmaker": bookmaker,
                            "id": str(item.get("id") or item.get("match_id") or ""),
                            "homeTeam": str(home),
                            "awayTeam": str(away),
                            "league": str(item.get("competition_name") or item.get("league") or "Soccer"),
                            "startTime": str(item.get("start_time") or ""),
                            "odds": {"1": o1, "X": oX, "2": o2}
                        })
            return matches

        # --- 2. SportyBet (tournaments -> events) ---
        if bookmaker == "sportybet":
            tournaments = data.get("data", {}).get("tournaments", []) if isinstance(data, dict) else []
            for tourney in tournaments:
                for item in tourney.get("events", []):
                    home = item.get("homeTeamName")
                    away = item.get("awayTeamName")
                    o1, oX, o2 = 1.0, 1.0, 1.0
                    for market in item.get("markets", []):
                        if market.get("id") == "1" or market.get("name") in ["1X2", "3-Way"]:
                            for outcome in market.get("outcomes", []):
                                desc = str(outcome.get("desc"))
                                if desc in ["1", "Home"]: o1 = safe_float(outcome.get("odds"))
                                elif desc in ["X", "Draw"]: oX = safe_float(outcome.get("odds"))
                                elif desc in ["2", "Away"]: o2 = safe_float(outcome.get("odds"))

                    if home and away:
                        matches.append({
                            "bookmaker": bookmaker,
                            "id": str(item.get("eventId") or ""),
                            "homeTeam": str(home),
                            "awayTeam": str(away),
                            "league": str(tourney.get("name") or "Soccer"),
                            "startTime": str(item.get("estimateStartTime") or ""),
                            "odds": {"1": o1, "X": oX, "2": o2}
                        })
            return matches

        # --- 3. BangBet (groupList -> matchVoList) ---
        if bookmaker == "bangbet":
            groups = data.get("data", {}).get("groupList", []) if isinstance(data, dict) else []
            for group in groups:
                for match_vo in group.get("matchVoList", []):
                    home = match_vo.get("homeTeamName")
                    away = match_vo.get("awayTeamName")
                    o1, oX, o2 = 1.0, 1.0, 1.0
                    for market in match_vo.get("marketList", []):
                        for m_item in market.get("markets", []):
                            for outcome in m_item.get("outcomes", []):
                                desc = str(outcome.get("desc") or outcome.get("name"))
                                if desc in ["1", home]: o1 = safe_float(outcome.get("odds"))
                                elif desc in ["X", "draw", "Draw"]: oX = safe_float(outcome.get("odds"))
                                elif desc in ["2", away]: o2 = safe_float(outcome.get("odds"))

                    if home and away:
                        matches.append({
                            "bookmaker": bookmaker,
                            "id": str(match_vo.get("matchId") or ""),
                            "homeTeam": str(home),
                            "awayTeam": str(away),
                            "league": str(group.get("groupName") or "Soccer"),
                            "startTime": str(match_vo.get("startTime") or ""),
                            "odds": {"1": o1, "X": oX, "2": o2}
                        })
            return matches

        # --- 4. Generic Fallback Engine ---
        events = []
        if isinstance(data, list):
            events = data
        elif isinstance(data, dict):
            events = data.get("data") or data.get("events") or data.get("matches") or data.get("items") or [data]

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
                        "id": str(item.get("id") or item.get("eventId") or ""),
                        "homeTeam": str(home),
                        "awayTeam": str(away),
                        "league": str(item.get("league") or "Soccer"),
                        "startTime": str(item.get("startTime") or ""),
                        "odds": {"1": o1, "X": oX, "2": o2}
                    })

    except Exception as e:
        logger.error(f"[{bookmaker}] Parser Exception: {repr(e)}")

    return matches


# -------------------------------------------------------------------
# INDIVIDUAL SITE ROUTINES
# -------------------------------------------------------------------

async def get_betika(client: httpx.AsyncClient):
    return await fetch_api(client, "https://api.betika.com/v1/uo/matches?limit=100&sub_type=prematch", "betika")

async def get_sportybet(client: httpx.AsyncClient):
    return await fetch_api(client, "https://www.sportybet.com/api/tz/factsCenter/pcUpcomingEvents?sportId=sr%3Asport%3A1&marketId=1%2C18%2C10%2C29%2C11%2C26%2C36%2C14%2C60100&pageSize=100&pageNum=1&option=1", "sportybet")

async def get_bangbet(client: httpx.AsyncClient):
    return await fetch_api(client, "https://bet-api.bangbet.com/api/bet/match/listTop?country=tz", "bangbet")

# ROUTE ALL ANTI-BOT / BLOCKED SITES THROUGH PLAYWRIGHT (STABLE CHROMIUM)
async def get_1xbet(client: httpx.AsyncClient):
    return await fetch_with_playwright("https://1xbet.tz/en/line/football", "1xbet")

async def get_22bet(client: httpx.AsyncClient):
    return await fetch_with_playwright("https://22bet.co.tz/line/football", "22bet")

async def get_helabet(client: httpx.AsyncClient):
    return await fetch_with_playwright("https://helabet.co.tz/line/football", "helabet")

async def get_sokabet(client: httpx.AsyncClient):
    return await fetch_with_playwright("https://sokabet.co.tz", "sokabet")

async def get_premierbet(client: httpx.AsyncClient):
    return await fetch_with_playwright("https://www.premierbet.co.tz", "premierbet")

async def get_betway(client: httpx.AsyncClient):
    return await fetch_with_playwright("https://www.betway.co.tz", "betway")

async def get_thronebet(client: httpx.AsyncClient):
    return await fetch_with_playwright("https://thronebet.com", "thronebet")

async def get_meridianbet(client: httpx.AsyncClient):
    return await fetch_with_playwright("https://meridianbet.co.tz", "meridianbet")

async def get_wasafibet(client: httpx.AsyncClient):
    return await fetch_with_playwright("https://wasafibet.com", "wasafibet")

async def get_gsb(client: httpx.AsyncClient):
    return await fetch_with_playwright("https://gsb.co.tz", "gsb")

async def get_kingbet(client: httpx.AsyncClient):
    return await fetch_with_playwright("https://kingbet.co.tz", "kingbet")

async def get_betpawa(client: httpx.AsyncClient):
    return await fetch_with_playwright("https://www.betpawa.co.tz", "betpawa")

async def get_888bet(client: httpx.AsyncClient):
    return await fetch_with_playwright("https://888bet.tz", "888bet")

async def get_parimatch(client: httpx.AsyncClient):
    return await fetch_with_playwright("https://parimatch.co.tz", "parimatch")

async def get_mostbet(client: httpx.AsyncClient):
    return await fetch_with_playwright("https://mostbet.co.tz", "mostbet")

async def get_1win(client: httpx.AsyncClient):
    return await fetch_with_playwright("https://1win.pro", "1win")

async def get_sportpesa(client: httpx.AsyncClient):
    return await fetch_api(client, "https://www.sportpesa.co.tz/api/games/highlights?sportId=1", "sportpesa")

async def get_leonbet(client: httpx.AsyncClient):
    return await fetch_api(client, "https://leonbet.co.tz/api-2/betline/events/all?ctag=en-US", "leonbet")


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
    async with httpx.AsyncClient(timeout=CUSTOM_TIMEOUT, follow_redirects=True) as client:
        tasks = [func(client) for func in BOOKMAKER_MAP.values()]
        results = await asyncio.gather(*tasks, return_exceptions=True)

    all_matches = []
    for res in results:
        if isinstance(res, list):
            all_matches.extend(res)
    return all_matches
