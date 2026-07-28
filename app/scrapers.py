import asyncio
import logging
import time
import httpx
from typing import Dict, List, Any, Optional
from playwright.async_api import async_playwright, Browser

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("scrapers")

REAL_BROWSER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
}

CUSTOM_TIMEOUT = httpx.Timeout(connect=15.0, read=20.0, write=15.0, pool=15.0)


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
                logger.info(f"[{bookmaker.upper()}] Parsed {len(matches)} matches.")
                return matches
            except Exception as parse_err:
                logger.error(f"[{bookmaker}] JSON Parse Error: {repr(parse_err)}")
        else:
            logger.warning(f"[{bookmaker}] HTTP Status {response.status_code}")
    except Exception as e:
        logger.error(f"[{bookmaker}] Fetch Error ({type(e).__name__}): {repr(e)}")
    return []


async def intercept_network_json(browser: Browser, url: str, bookmaker: str, url_keywords: List[str], wait_time: int = 5) -> List[Dict[str, Any]]:
    captured_payloads = []
    all_matches = []

    try:
        context = await browser.new_context(user_agent=REAL_BROWSER_HEADERS["User-Agent"])
        page = await context.new_page()

        async def handle_response(response):
            res_url = response.url.lower()
            if any(kw in res_url for kw in url_keywords):
                try:
                    ct = response.headers.get("content-type", "")
                    if "json" in ct or "text/plain" in ct:
                        json_data = await response.json()
                        captured_payloads.append(json_data)
                except Exception:
                    pass

        page.on("response", handle_response)
        await page.goto(url, wait_until="domcontentloaded", timeout=25000)
        await asyncio.sleep(wait_time)
        
        await page.close()
        await context.close()

        for payload in captured_payloads:
            parsed = parse_raw_data(bookmaker, payload)
            all_matches.extend(parsed)

        unique_matches = list({m["match_id"]: m for m in all_matches if m.get("match_id")}.values()) if all_matches else []
        logger.info(f"[{bookmaker.upper()}-INTERCEPTOR] Parsed {len(unique_matches)} unique matches.")
        return unique_matches

    except Exception as e:
        logger.error(f"[{bookmaker}-INTERCEPTOR] Error ({type(e).__name__}): {repr(e)}")
    return []


# -------------------------------------------------------------------
# NORMALIZED PARSER ENGINE (Android APK Schema Compliant)
# -------------------------------------------------------------------

def parse_raw_data(bookmaker: str, data: Any) -> List[Dict[str, Any]]:
    matches = []
    ts = int(time.time())
    try:
        # --- 1. BETIKA ---
        if bookmaker == "betika":
            events = data.get("data", []) if isinstance(data, dict) else (data if isinstance(data, list) else [])
            for item in events:
                if isinstance(item, dict):
                    home = item.get("home_team")
                    away = item.get("away_team")
                    if home and away:
                        matches.append({
                            "match_id": str(item.get("match_id") or item.get("game_id") or ""),
                            "home_team": str(home),
                            "away_team": str(away),
                            "competition": str(item.get("competition_name") or "Soccer"),
                            "home_odds": safe_float(item.get("home_odd")),
                            "draw_odds": safe_float(item.get("neutral_odd")),
                            "away_odds": safe_float(item.get("away_odd")),
                            "bookmaker_id": bookmaker,
                            "timestamp": ts,
                        })
            return matches

        # --- 2. 1XCORP ENGINE CLONES (1xbet, 22bet, helabet, mostbet, betwinner, melbet, megapari, 1xbit) ---
        if bookmaker in ["1xbet", "22bet", "helabet", "mostbet", "betwinner", "melbet", "megapari", "1xbit"]:
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
                            "match_id": str(item.get("I") or ""),
                            "home_team": str(home),
                            "away_team": str(away),
                            "competition": str(item.get("LE") or "Soccer"),
                            "home_odds": o1,
                            "draw_odds": oX,
                            "away_odds": o2,
                            "bookmaker_id": bookmaker,
                            "timestamp": ts,
                        })
            return matches

        # --- 3. SPORTYBET ---
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
                            "match_id": str(item.get("eventId") or ""),
                            "home_team": str(home),
                            "away_team": str(away),
                            "competition": str(tourney.get("name") or "Soccer"),
                            "home_odds": o1,
                            "draw_odds": oX,
                            "away_odds": o2,
                            "bookmaker_id": bookmaker,
                            "timestamp": ts,
                        })
            return matches

        # --- 4. BANGBET ---
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
                            "match_id": str(match_vo.get("matchId") or ""),
                            "home_team": str(home),
                            "away_team": str(away),
                            "competition": str(group.get("groupName") or "Soccer"),
                            "home_odds": o1,
                            "draw_odds": oX,
                            "away_odds": o2,
                            "bookmaker_id": bookmaker,
                            "timestamp": ts,
                        })
            return matches

        # --- 5. LEONBET ---
        if bookmaker == "leonbet":
            events = data.get("events", []) if isinstance(data, dict) else []
            for item in events:
                if isinstance(item, dict):
                    home = item.get("homeTeam", {}).get("name") or item.get("home")
                    away = item.get("awayTeam", {}).get("name") or item.get("away")
                    o1, oX, o2 = 1.0, 1.0, 1.0
                    for runner in item.get("runners", []):
                        tags = runner.get("tags", [])
                        price = runner.get("price")
                        if "HOME" in tags or runner.get("type") == "1": o1 = safe_float(price)
                        elif "DRAW" in tags or runner.get("type") == "X": oX = safe_float(price)
                        elif "AWAY" in tags or runner.get("type") == "2": o2 = safe_float(price)

                    if home and away:
                        matches.append({
                            "match_id": str(item.get("id") or ""),
                            "home_team": str(home),
                            "away_team": str(away),
                            "competition": str(item.get("league", {}).get("name") or "Soccer"),
                            "home_odds": o1,
                            "draw_odds": oX,
                            "away_odds": o2,
                            "bookmaker_id": bookmaker,
                            "timestamp": ts,
                        })
            return matches

        # --- 6. GENERIC FALLBACK ENGINE ---
        events = data if isinstance(data, list) else (data.get("data") or data.get("events") or data.get("matches") or data.get("items") or [data] if isinstance(data, dict) else [])
        for item in events:
            if isinstance(item, dict):
                home = item.get("homeTeam") or item.get("home_team") or item.get("homeName") or item.get("team1")
                away = item.get("awayTeam") or item.get("away_team") or item.get("awayName") or item.get("team2")
                raw_odds = item.get("odds") or {}
                o1 = safe_float(item.get("home_odds") or item.get("homeOdds") or item.get("odds1") or raw_odds.get("1"))
                oX = safe_float(item.get("draw_odds") or item.get("drawOdds") or item.get("oddsX") or raw_odds.get("X"))
                o2 = safe_float(item.get("away_odds") or item.get("awayOdds") or item.get("odds2") or raw_odds.get("2"))

                if home and away:
                    matches.append({
                        "match_id": str(item.get("id") or item.get("eventId") or item.get("match_id") or ""),
                        "home_team": str(home),
                        "away_team": str(away),
                        "competition": str(item.get("league") or item.get("competition") or "Soccer"),
                        "home_odds": o1,
                        "draw_odds": oX,
                        "away_odds": o2,
                        "bookmaker_id": bookmaker,
                        "timestamp": ts,
                    })

    except Exception as e:
        logger.error(f"[{bookmaker}] Parser Exception: {repr(e)}")

    return matches


# -------------------------------------------------------------------
# INDIVIDUAL BOOKMAKER ROUTINES (33 TOTAL)
# -------------------------------------------------------------------

# --- TIER 1: FAST API SCRAPERS ---
async def fetch_betika_odds(c: httpx.AsyncClient): return await fetch_api(c, "https://api.betika.com/v1/uo/matches?limit=100&sub_type=prematch", "betika")
async def fetch_sportybet_odds(c: httpx.AsyncClient): return await fetch_api(c, "https://www.sportybet.com/api/tz/factsCenter/pcUpcomingEvents?sportId=sr%3Asport%3A1&marketId=1%2C18%2C10%2C29%2C11%2C26%2C36%2C14%2C60100&pageSize=100&pageNum=1&option=1", "sportybet")
async def fetch_bangbet_odds(c: httpx.AsyncClient): return await fetch_api(c, "https://bet-api.bangbet.com/api/bet/match/listTop?country=tz", "bangbet")
async def fetch_sportpesa_odds(c: httpx.AsyncClient): return await fetch_api(c, "https://www.sportpesa.co.tz/api/upcoming/games?sportId=1", "sportpesa")
async def fetch_leonbet_odds(c: httpx.AsyncClient): return await fetch_api(c, "https://leonbet.co.tz/api-2/betline/events/all?ctag=en-US", "leonbet")
async def fetch_betpawa_odds(c: httpx.AsyncClient): return await fetch_api(c, "https://www.betpawa.co.tz/api/sportsbook/v1/events?sportId=1", "betpawa")
async def fetch_gsb_odds(c: httpx.AsyncClient): return await fetch_api(c, "https://gsb.co.tz/services/evapi/event/GetSportsTree?statusId=0&eventTypeId=0", "gsb")
async def fetch_premierbet_odds(c: httpx.AsyncClient): return await fetch_api(c, "https://sports-api.premierbet.co.tz/v1/events/highlights?country=TZ&group=g2&platform=desktop&locale=sw&sportId=1&limit=20", "premierbet")
async def fetch_mozzartbet_odds(c: httpx.AsyncClient): return await fetch_api(c, "https://mozzartbet.co.tz/m3-sport-api/v1/events/active", "mozzartbet")
async def fetch_mbet_odds(c: httpx.AsyncClient): return await fetch_api(c, "https://m-bet.co.tz/api/sportsbook/highlights", "mbet")
async def fetch_odibets_odds(c: httpx.AsyncClient): return await fetch_api(c, "https://odibets.co.tz/api/v1/matches/highlights", "odibets")

# --- TIER 2: 1XBET ENGINE CLONES ---
async def fetch_1xbet_odds(c: httpx.AsyncClient): return await fetch_api(c, "https://1xbet.tz/service-api/main-line-feed/v1/expressDay?cfView=3&country=181&gr=1499&lng=en", "1xbet")
async def fetch_22bet_odds(c: httpx.AsyncClient): return await fetch_api(c, "https://22bet.co.tz/service-api/main-line-feed/v1/expressDay?cfView=3&country=181&gr=1499&lng=en", "22bet")
async def fetch_helabet_odds(c: httpx.AsyncClient): return await fetch_api(c, "https://helabet.co.tz/service-api/main-line-feed/v1/expressDay?cfView=3&country=181&gr=772&lng=en", "helabet")
async def fetch_mostbet_odds(c: httpx.AsyncClient): return await fetch_api(c, "https://mostbet.co.tz/api/v1/line?sportId=1", "mostbet")
async def fetch_betwinner_odds(c: httpx.AsyncClient): return await fetch_api(c, "https://betwinner.co.tz/service-api/main-line-feed/v1/expressDay?cfView=3&country=181&lng=en", "betwinner")
async def fetch_melbet_odds(c: httpx.AsyncClient): return await fetch_api(c, "https://melbet.co.tz/service-api/main-line-feed/v1/expressDay?cfView=3&country=181&lng=en", "melbet")
async def fetch_megapari_odds(c: httpx.AsyncClient): return await fetch_api(c, "https://megapari.co.tz/service-api/main-line-feed/v1/expressDay?cfView=3&country=181&lng=en", "megapari")
async def fetch_1xbit_odds(c: httpx.AsyncClient): return await fetch_api(c, "https://1xbit.com/service-api/main-line-feed/v1/expressDay?cfView=3&lng=en", "1xbit")


FAST_API_BOOKMAKERS = {
    "betika": fetch_betika_odds,
    "sportybet": fetch_sportybet_odds,
    "bangbet": fetch_bangbet_odds,
    "sportpesa": fetch_sportpesa_odds,
    "leonbet": fetch_leonbet_odds,
    "betpawa": fetch_betpawa_odds,
    "gsb": fetch_gsb_odds,
    "premierbet": fetch_premierbet_odds,
    "mozzartbet": fetch_mozzartbet_odds,
    "mbet": fetch_mbet_odds,
    "odibets": fetch_odibets_odds,
}

ONEX_ENGINE_BOOKMAKERS = {
    "1xbet": fetch_1xbet_odds,
    "22bet": fetch_22bet_odds,
    "helabet": fetch_helabet_odds,
    "mostbet": fetch_mostbet_odds,
    "betwinner": fetch_betwinner_odds,
    "melbet": fetch_melbet_odds,
    "megapari": fetch_megapari_odds,
    "1xbit": fetch_1xbit_odds,
}

# --- TIER 3: PLAYWRIGHT SITES (Anti-Bot / Protected) ---
PLAYWRIGHT_SITES = [
    ("parimatch", "https://parimatch.co.tz", ["prematch", "sports", "v1"]),
    ("betway", "https://www.betway.co.tz", ["highlights", "betbook", "sportsapi"]),
    ("meridianbet", "https://meridianbet.co.tz", ["events", "highlights", "api"]),
    ("sokabet", "https://sokabet.co.tz", ["gettopevents", "events", "sportsbook"]),
    ("888bet", "https://888bet.tz", ["sportsbook", "league-card", "highlights"]),
    ("1win", "https://1win.pro", ["sports", "events", "v1"]),
    ("wasafibet", "https://wasafibet.com", ["sportsbook", "matches", "wb"]),
    ("kingbet", "https://kingbet.co.tz", ["events", "redis_data", "sports"]),
    ("thronebet", "https://thronebet.com", ["multi", "v2", "api"]),
    ("pmbet", "https://pmbet.co.tz", ["events", "highlights"]),
    ("10bet", "https://10bet.co.tz", ["sports", "events"]),
    ("winprincess", "https://winprincess.co.tz", ["events", "sportsbook"]),
    ("playmaster", "https://playmaster.co.tz", ["events", "sports"]),
    ("betafriq", "https://betafriq.co.tz", ["events", "highlights"]),
]

PLAYWRIGHT_BOOKMAKERS = {bm: None for bm, _, _ in PLAYWRIGHT_SITES}

# MASTER MAP FOR FASTAPI ROUTER & APPLICATION SCANNER
BOOKMAKER_MAP = {
    **FAST_API_BOOKMAKERS,
    **ONEX_ENGINE_BOOKMAKERS,
    **PLAYWRIGHT_BOOKMAKERS,
}


async def scrape_all_sportsbooks() -> List[Dict[str, Any]]:
    all_matches = []

    # TIER 1 & TIER 2: FAST DIRECT HTTP CALLS
    async with httpx.AsyncClient(timeout=CUSTOM_TIMEOUT, follow_redirects=True) as client:
        fast_funcs = [func for func in {**FAST_API_BOOKMAKERS, **ONEX_ENGINE_BOOKMAKERS}.values() if func]
        tasks = [func(client) for func in fast_funcs]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for res in results:
            if isinstance(res, list):
                all_matches.extend(res)

    # TIER 3: PLAYWRIGHT INTERCEPTORS (SEQUENTIAL BATCHING)
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                args=["--disable-blink-features=AutomationControlled", "--disable-dev-shm-usage", "--no-sandbox"]
            )
            for bm, url, keywords in PLAYWRIGHT_SITES:
                res = await intercept_network_json(browser, url, bm, keywords)
                if isinstance(res, list):
                    all_matches.extend(res)
            await browser.close()
    except Exception as e:
        logger.error(f"Playwright Execution Error: {repr(e)}")

    return all_matches
