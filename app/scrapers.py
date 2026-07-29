import asyncio
import logging
import time
import json
import httpx
from typing import Dict, List, Any, Optional
from playwright.async_api import async_playwright, Browser

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("scrapers")

REAL_BROWSER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate",
}

CUSTOM_TIMEOUT = httpx.Timeout(connect=15.0, read=20.0, write=15.0, pool=15.0)

HTTP_SEMAPHORE = asyncio.Semaphore(6)
PLAYWRIGHT_SEMAPHORE = asyncio.Semaphore(3)


def safe_float(val: Any, default: float = 1.0) -> float:
    try:
        if val is None:
            return default
        parsed = float(val)
        return parsed if parsed > 1.01 else default
    except (ValueError, TypeError):
        return default


def validate_match(match: Dict[str, Any]) -> bool:
    if len(match.get("home_team", "")) < 2 or len(match.get("away_team", "")) < 2:
        return False
    if match.get("home_odds", 1.0) <= 1.01 and match.get("away_odds", 1.0) <= 1.01:
        return False
    return True


# -------------------------------------------------------------------
# BOOKMAKER REGISTRY
# -------------------------------------------------------------------

BOOKMAKER_REGISTRY = {
    # Tier 1: Fast REST APIs
    "betika": {"platform": "public_rest", "url": "https://api.betika.com/v1/uo/matches?limit=100&sub_type=prematch", "parser": "betika"},
    "sportybet": {"platform": "public_rest", "url": "https://www.sportybet.com/api/tz/factsCenter/pcUpcomingEvents?sportId=sr%3Asport%3A1&marketId=1%2C18%2C10%2C29%2C11%2C26%2C36%2C14%2C60100&pageSize=100&pageNum=1&option=1", "parser": "sportybet"},
    "bangbet": {"platform": "public_rest", "url": "https://bet-api.bangbet.com/api/bet/match/listTop?country=tz", "parser": "bangbet"},
    "sportpesa": {"platform": "public_rest", "url": "https://www.sportpesa.co.tz/api/games/highlights?sportId=1&version=v2", "parser": "sportpesa"},
    "leonbet": {"platform": "public_rest", "url": "https://leonbet.co.tz/api-2/betline/events/all?ctag=en-US", "parser": "leonbet"},
    "premierbet": {"platform": "public_rest", "url": "https://sports-api.premierbet.co.tz/v1/events/highlights?country=TZ&group=g2&platform=desktop&locale=en&sportId=1&limit=50", "parser": "premierbet"},

    # Tier 2: Protected / Intercepted Platforms (Playwright Network Interceptors)
    "meridianbet": {"platform": "playwright_spa", "url": "https://meridianbet.co.tz/en/betting/football", "keywords": ["betsapi", "events", "standard", "v2", "sports"], "parser": "meridianbet"},
    "1xbet": {"platform": "playwright_spa", "url": "https://1xbet.co.tz/en/line/football", "keywords": ["get1x2", "compresszip", "getclubslinezip", "linefeed", "sports", "main"], "parser": "1xcorp"},
    "22bet": {"platform": "playwright_spa", "url": "https://22bet.co.tz/en/line/football", "keywords": ["get1x2", "compresszip", "linefeed", "sports"], "parser": "1xcorp"},
    "helabet": {"platform": "playwright_spa", "url": "https://helabet.co.tz/en/line/football", "keywords": ["get1x2", "compresszip", "linefeed", "sports"], "parser": "1xcorp"},
    "betwinner": {"platform": "playwright_spa", "url": "https://betwinner.co.tz/en/line/football", "keywords": ["get1x2", "compresszip", "linefeed", "sports"], "parser": "1xcorp"},
    "melbet": {"platform": "playwright_spa", "url": "https://melbet.co.tz/en/line/football", "keywords": ["get1x2", "compresszip", "linefeed", "sports"], "parser": "1xcorp"},
    "1xbit": {"platform": "playwright_spa", "url": "https://1xbit.com/en/line/football", "keywords": ["get1x2", "compresszip", "linefeed", "sports"], "parser": "1xcorp"},
    
    "parimatch": {"platform": "playwright_spa", "url": "https://parimatch.co.tz/en/football", "keywords": ["prematch", "sports", "events", "v1", "line", "apg", "sportsbook"], "parser": "generic"},
    "betway": {"platform": "playwright_spa", "url": "https://www.betway.co.tz/sport/soccer", "keywords": ["highlights", "betbook", "sportsapi", "event", "soccer"], "parser": "generic"},
    "sokabet": {"platform": "playwright_spa", "url": "https://sokabet.co.tz", "keywords": ["gettopevents", "events", "sportsbook", "gettop"], "parser": "generic"},
    "888bet": {"platform": "playwright_spa", "url": "https://888bet.tz/en/sports/football", "keywords": ["sportsbook", "league-card", "highlights", "api", "events"], "parser": "generic"},
    "1win": {"platform": "playwright_spa", "url": "https://1win.pro/bets/home", "keywords": ["sports", "events", "v1", "line"], "parser": "generic"},
    "wasafibet": {"platform": "playwright_spa", "url": "https://wasafibet.com", "keywords": ["sportsbook", "matches", "wb"], "parser": "generic"},
    "kingbet": {"platform": "playwright_spa", "url": "https://kingbet.co.tz", "keywords": ["events", "redis_data", "sports"], "parser": "generic"},
    "thronebet": {"platform": "playwright_spa", "url": "https://thronebet.com", "keywords": ["multi", "v2", "api"], "parser": "generic"},
}

BOOKMAKER_MAP = {bm: None for bm in BOOKMAKER_REGISTRY.keys()}


# -------------------------------------------------------------------
# PAYLOAD FINGERPRINTING & DIAGNOSTIC PARSER ENGINE
# -------------------------------------------------------------------

def auto_detect_parser(payload: Any) -> str:
    if isinstance(payload, dict):
        if "Value" in payload or ("O1" in str(payload) and "LE" in str(payload)):
            return "1xcorp"
        if "home_team" in str(payload) and "home_odd" in str(payload):
            return "betika"
        if "tournaments" in payload.get("data", {}):
            return "sportybet"
        if "groupList" in payload.get("data", {}):
            return "bangbet"
        if "events" in payload and ("runners" in str(payload) or "markets" in str(payload)):
            return "leonbet"
        if "games" in str(payload) and "leagueName" in str(payload):
            return "meridianbet"
    return "generic"


def parse_raw_payload(bookmaker_id: str, payload: Any, latency_ms: int = 0) -> List[Dict[str, Any]]:
    ts = int(time.time())
    config = BOOKMAKER_REGISTRY.get(bookmaker_id, {})
    parser_type = config.get("parser")

    detected = auto_detect_parser(payload)
    if detected != "generic":
        parser_type = detected

    raw_parsed = []

    try:
        # 1. BETIKA
        if parser_type == "betika":
            events = payload.get("data", []) if isinstance(payload, dict) else (payload if isinstance(payload, list) else [])
            for item in events:
                if isinstance(item, dict):
                    raw_parsed.append({
                        "match_id": str(item.get("match_id") or item.get("game_id") or ""),
                        "home_team": str(item.get("home_team") or ""),
                        "away_team": str(item.get("away_team") or ""),
                        "competition": str(item.get("competition_name") or "Soccer"),
                        "home_odds": safe_float(item.get("home_odd")),
                        "draw_odds": safe_float(item.get("neutral_odd")),
                        "away_odds": safe_float(item.get("away_odd")),
                        "sport": "soccer", "market_type": "1X2",
                        "bookmaker_id": bookmaker_id, "timestamp": ts, "latency_ms": latency_ms
                    })

        # 2. LEONBET
        elif parser_type == "leonbet":
            events = payload.get("events", []) if isinstance(payload, dict) else []
            if not events and isinstance(payload, dict):
                events = payload.get("data", {}).get("events", []) or payload.get("items", [])
            for item in events:
                if isinstance(item, dict):
                    home = item.get("homeTeam", {}).get("name") or item.get("home")
                    away = item.get("awayTeam", {}).get("name") or item.get("away")
                    o1, oX, o2 = 1.0, 1.0, 1.0

                    runners = item.get("runners", [])
                    if not runners and item.get("markets"):
                        for market in item.get("markets", []):
                            if market.get("name") in ["1X2", "Match Winner", "Winner"]:
                                runners = market.get("runners", [])
                                break

                    for runner in runners:
                        tags = runner.get("tags", [])
                        price = runner.get("price") or runner.get("odd")
                        r_type = str(runner.get("type") or runner.get("name"))
                        if "HOME" in tags or r_type in ["1", "Home"]: o1 = safe_float(price)
                        elif "DRAW" in tags or r_type in ["X", "Draw"]: oX = safe_float(price)
                        elif "AWAY" in tags or r_type in ["2", "Away"]: o2 = safe_float(price)

                    raw_parsed.append({
                        "match_id": str(item.get("id") or ""),
                        "home_team": str(home or ""), "away_team": str(away or ""),
                        "competition": str(item.get("league", {}).get("name") or "Soccer"),
                        "home_odds": o1, "draw_odds": oX, "away_odds": o2,
                        "sport": "soccer", "market_type": "1X2",
                        "bookmaker_id": bookmaker_id, "timestamp": ts, "latency_ms": latency_ms
                    })

        # 3. SPORTYBET
        elif parser_type == "sportybet":
            tournaments = payload.get("data", {}).get("tournaments", []) if isinstance(payload, dict) else []
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

                    raw_parsed.append({
                        "match_id": str(item.get("eventId") or ""),
                        "home_team": str(home or ""), "away_team": str(away or ""),
                        "competition": str(tourney.get("name") or "Soccer"),
                        "home_odds": o1, "draw_odds": oX, "away_odds": o2,
                        "sport": "soccer", "market_type": "1X2",
                        "bookmaker_id": bookmaker_id, "timestamp": ts, "latency_ms": latency_ms
                    })

        # 4. PREMIERBET
        elif parser_type == "premierbet":
            events = payload if isinstance(payload, list) else (payload.get("events") or payload.get("data") or payload.get("items") or [])
            for item in events:
                if isinstance(item, dict):
                    home = item.get("homeTeam") or item.get("home_team")
                    away = item.get("awayTeam") or item.get("away_team")
                    o1, oX, o2 = 1.0, 1.0, 1.0
                    for market in item.get("markets", []):
                        for selection in market.get("selections", []):
                            name = str(selection.get("name"))
                            price = safe_float(selection.get("price") or selection.get("odds"))
                            if name in ["1", "Home"]: o1 = price
                            elif name in ["X", "Draw"]: oX = price
                            elif name in ["2", "Away"]: o2 = price

                    raw_parsed.append({
                        "match_id": str(item.get("id") or ""),
                        "home_team": str(home or ""), "away_team": str(away or ""),
                        "competition": str(item.get("tournament") or "Soccer"),
                        "home_odds": o1, "draw_odds": oX, "away_odds": o2,
                        "sport": "soccer", "market_type": "1X2",
                        "bookmaker_id": bookmaker_id, "timestamp": ts, "latency_ms": latency_ms
                    })

        # 5. 1XCORP PARSER
        elif parser_type == "1xcorp":
            events = payload.get("Value", []) if isinstance(payload, dict) else []
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

                    raw_parsed.append({
                        "match_id": str(item.get("I") or ""),
                        "home_team": str(home or ""), "away_team": str(away or ""),
                        "competition": str(item.get("LE") or "Soccer"),
                        "home_odds": o1, "draw_odds": oX, "away_odds": o2,
                        "sport": "soccer", "market_type": "1X2",
                        "bookmaker_id": bookmaker_id, "timestamp": ts, "latency_ms": latency_ms
                    })

        # 6. GENERIC FALLBACK
        else:
            events = payload if isinstance(payload, list) else (payload.get("data") or payload.get("events") or payload.get("matches") or [payload] if isinstance(payload, dict) else [])
            for item in events:
                if isinstance(item, dict):
                    home = item.get("homeTeam") or item.get("home_team") or item.get("homeName") or item.get("team1")
                    away = item.get("awayTeam") or item.get("away_team") or item.get("awayName") or item.get("team2")
                    raw_odds = item.get("odds") or {}
                    o1 = safe_float(item.get("home_odds") or item.get("homeOdds") or item.get("odds1") or raw_odds.get("1"))
                    oX = safe_float(item.get("draw_odds") or item.get("drawOdds") or item.get("oddsX") or raw_odds.get("X"))
                    o2 = safe_float(item.get("away_odds") or item.get("awayOdds") or item.get("odds2") or raw_odds.get("2"))

                    raw_parsed.append({
                        "match_id": str(item.get("id") or item.get("eventId") or item.get("match_id") or ""),
                        "home_team": str(home or ""), "away_team": str(away or ""),
                        "competition": str(item.get("league") or item.get("competition") or "Soccer"),
                        "home_odds": o1, "draw_odds": oX, "away_odds": o2,
                        "sport": "soccer", "market_type": "1X2",
                        "bookmaker_id": bookmaker_id, "timestamp": ts, "latency_ms": latency_ms
                    })

        # APPLY STRICT VALIDATOR
        matches = [m for m in raw_parsed if validate_match(m)]

        # --- DIAGNOSTIC PAYLOAD DUMP ON 0 PARSED MATCHES ---
        if len(matches) == 0:
            if isinstance(payload, dict):
                top_keys = list(payload.keys())[:10]
                sample_str = json.dumps(payload)[:300]
                logger.warning(f"[{bookmaker_id.upper()}-SCHEMA-MISMATCH] Top keys: {top_keys} | Sample: {sample_str}")
            elif isinstance(payload, list):
                logger.warning(f"[{bookmaker_id.upper()}-SCHEMA-MISMATCH] Returned List length: {len(payload)}")

    except Exception as e:
        logger.error(f"[{bookmaker_id}] Parser Exception ({type(e).__name__}): {repr(e)}")

    return matches


# -------------------------------------------------------------------
# CONCURRENT NETWORK FETCHERS WITH SEMAPHORES
# -------------------------------------------------------------------

async def fetch_http_api(client: httpx.AsyncClient, bookmaker_id: str, config: dict) -> List[Dict[str, Any]]:
    url = config["url"]
    async with HTTP_SEMAPHORE:
        start_t = time.time()
        try:
            response = await client.get(url, headers=REAL_BROWSER_HEADERS, timeout=CUSTOM_TIMEOUT)
            latency_ms = int((time.time() - start_t) * 1000)
            if response.status_code in [200, 203]:
                data = response.json()
                matches = parse_raw_payload(bookmaker_id, data, latency_ms=latency_ms)
                logger.info(f"[{bookmaker_id.upper()}] Parsed {len(matches)} valid matches in {latency_ms}ms.")
                return matches
            else:
                logger.warning(f"[{bookmaker_id}] HTTP Status {response.status_code}")
        except Exception as e:
            logger.error(f"[{bookmaker_id}] Fetch Error ({type(e).__name__}): {repr(e)}")
    return []


async def intercept_playwright_spa(browser: Browser, bookmaker_id: str, config: dict, wait_time: int = 5) -> List[Dict[str, Any]]:
    url = config["url"]
    keywords = config.get("keywords", [])
    bm_label = bookmaker_id.upper()
    captured_payloads = []
    all_matches = []

    async with PLAYWRIGHT_SEMAPHORE:
        start_t = time.time()
        try:
            context = await browser.new_context(
                user_agent=REAL_BROWSER_HEADERS["User-Agent"],
                viewport={"width": 1280, "height": 720}
            )
            page = await context.new_page()

            async def handle_response(response):
                if response.status == 200:
                    res_url = response.url.lower()
                    if any(kw in res_url for kw in keywords):
                        try:
                            json_data = await response.json()
                            captured_payloads.append((response.url, json_data))
                        except Exception:
                            pass

            page.on("response", handle_response)
            logger.info(f"[{bm_label}-INTERCEPTOR] Navigating to {url}...")
            
            await page.goto(url, wait_until="domcontentloaded", timeout=20000)
            try:
                await page.evaluate("window.scrollBy(0, 500)")
            except Exception:
                pass

            await asyncio.sleep(wait_time)
            await page.close()
            await context.close()

            latency_ms = int((time.time() - start_t) * 1000)

            for res_url, payload in captured_payloads:
                parsed = parse_raw_payload(bookmaker_id, payload, latency_ms=latency_ms)
                all_matches.extend(parsed)

            unique_matches = list({m["match_id"]: m for m in all_matches if m.get("match_id")}.values()) if all_matches else []
            logger.info(f"[{bm_label}-INTERCEPTOR] Parsed {len(unique_matches)} matches in {latency_ms}ms.")
            return unique_matches

        except Exception as e:
            logger.error(f"[{bookmaker_id}-INTERCEPTOR] Error ({type(e).__name__}): {repr(e)}")
    return []


# -------------------------------------------------------------------
# DISPATCHER MASTER SCANNER LOOP
# -------------------------------------------------------------------

async def scrape_all_sportsbooks() -> List[Dict[str, Any]]:
    all_matches = []

    # 1. Parallel HTTP Scrapers (Managed by HTTP_SEMAPHORE)
    http_targets = {bm: cfg for bm, cfg in BOOKMAKER_REGISTRY.items() if cfg["platform"] in ["public_rest"]}
    
    async with httpx.AsyncClient(timeout=CUSTOM_TIMEOUT, follow_redirects=True) as client:
        http_tasks = [fetch_http_api(client, bm, cfg) for bm, cfg in http_targets.items()]
        http_results = await asyncio.gather(*http_tasks, return_exceptions=True)
        for res in http_results:
            if isinstance(res, list):
                all_matches.extend(res)

    # 2. Concurrent Playwright Workers (Managed by PLAYWRIGHT_SEMAPHORE)
    playwright_targets = {bm: cfg for bm, cfg in BOOKMAKER_REGISTRY.items() if cfg["platform"] == "playwright_spa"}

    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                args=["--disable-blink-features=AutomationControlled", "--disable-dev-shm-usage", "--no-sandbox"]
            )
            pw_tasks = [intercept_playwright_spa(browser, bm, cfg) for bm, cfg in playwright_targets.items()]
            pw_results = await asyncio.gather(*pw_tasks, return_exceptions=True)
            for res in pw_results:
                if isinstance(res, list):
                    all_matches.extend(res)
            await browser.close()
    except Exception as e:
        logger.error(f"Playwright Execution Batch Error: {repr(e)}")

    return all_matches
