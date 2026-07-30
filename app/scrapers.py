import asyncio
import logging
import time
import json
from typing import Dict, List, Any, Optional

from curl_cffi.requests import AsyncSession
from playwright.async_api import async_playwright, Browser

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("scrapers")

REAL_BROWSER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate",
}

# Concurrency tuning
HTTP_SEMAPHORE = asyncio.Semaphore(15)
PLAYWRIGHT_SEMAPHORE = asyncio.Semaphore(6)  # Bumped to 6 for faster multi-site execution


def safe_float(val: Any, default: float = 1.0) -> float:
    try:
        if val is None:
            return default
        parsed = float(val)
        return parsed if parsed > 1.01 else default
    except (ValueError, TypeError):
        return default


def validate_match(match: Dict[str, Any]) -> bool:
    """
    Relaxed match validation: requires team names and valid Home/Away odds (> 1.01).
    Draw odds default to 1.0 if missing, preventing rejection of valid event pairs.
    """
    if len(match.get("home_team", "").strip()) < 2 or len(match.get("away_team", "").strip()) < 2:
        return False
    
    o1 = match.get("home_odds", 1.0)
    o2 = match.get("away_odds", 1.0)

    # Require Home and Away odds > 1.01
    return o1 > 1.01 and o2 > 1.01


# -------------------------------------------------------------------
# BOOKMAKER REGISTRY (33 TARGETS)
# -------------------------------------------------------------------

BOOKMAKER_REGISTRY = {
    # Tier 1: Fast REST APIs (TLS Impersonation)
    "betika": {"platform": "public_rest", "url": "https://api.betika.com/v1/uo/matches?limit=100&sub_type=prematch", "parser": "betika"},
    "sportybet": {"platform": "public_rest", "url": "https://www.sportybet.com/api/tz/factsCenter/pcUpcomingEvents?sportId=sr%3Asport%3A1&marketId=1%2C18%2C10%2C29%2C11%2C26%2C36%2C14%2C60100&pageSize=100&pageNum=1&option=1", "parser": "sportybet"},
    "bangbet": {"platform": "public_rest", "url": "https://bet-api.bangbet.com/api/bet/match/listTop?country=tz", "parser": "bangbet"},
    "sportpesa": {"platform": "public_rest", "url": "https://www.sportpesa.co.tz/api/games/highlights?sportId=1&version=v2", "parser": "sportpesa"},
    "leonbet": {"platform": "public_rest", "url": "https://leonbet.co.tz/api-2/betline/events/all?ctag=en-US", "parser": "leonbet"},
    "premierbet": {"platform": "public_rest", "url": "https://sports-api.premierbet.co.tz/v1/events/highlights?country=TZ&group=g2&platform=desktop&locale=en&sportId=1&limit=50", "parser": "premierbet"},
    "galsport": {"platform": "public_rest", "url": "https://galsport.co.tz/api/v1/sportsbook/highlights?sportId=1", "parser": "generic"},
    "playmaster": {"platform": "public_rest", "url": "https://playmaster.co.tz/api/v1/events/top", "parser": "generic"},

    # Tier 2: 1XCorp Clone Family (Playwright Interception)
    "1xbet": {"platform": "playwright_spa", "url": "https://1xbet.co.tz/en/line/football", "keywords": ["bff-api", "get1x2", "compresszip", "getclubslinezip", "linefeed", "livefeed", "line", "events", "zip"], "parser": "1xcorp"},
    "22bet": {"platform": "playwright_spa", "url": "https://22bet.co.tz/en/line/football", "keywords": ["bff-api", "get1x2", "compresszip", "linefeed", "line", "events", "zip"], "parser": "1xcorp"},
    "helabet": {"platform": "playwright_spa", "url": "https://helabet.co.tz/en/line/football", "keywords": ["bff-api", "get1x2", "compresszip", "linefeed", "line", "events", "zip"], "parser": "1xcorp"},
    "betwinner": {"platform": "playwright_spa", "url": "https://betwinner.co.tz/en/line/football", "keywords": ["bff-api", "get1x2", "compresszip", "linefeed", "line", "events", "zip"], "parser": "1xcorp"},
    "melbet": {"platform": "playwright_spa", "url": "https://melbet.co.tz/en/line/football", "keywords": ["bff-api", "get1x2", "compresszip", "linefeed", "line", "events", "zip"], "parser": "1xcorp"},
    "1xbit": {"platform": "playwright_spa", "url": "https://1xbit.com/en/line/football", "keywords": ["bff-api", "get1x2", "compresszip", "linefeed", "line", "events", "zip"], "parser": "1xcorp"},
    "888starz": {"platform": "playwright_spa", "url": "https://888starz.bet/en/line/football", "keywords": ["bff-api", "get1x2", "compresszip", "linefeed", "line", "events"], "parser": "1xcorp"},
    "megapari": {"platform": "playwright_spa", "url": "https://megapari.com/en/line/football", "keywords": ["bff-api", "get1x2", "compresszip", "linefeed", "line", "events"], "parser": "1xcorp"},

    # Tier 3: Additional Protected / SPA Sportsbooks
    "meridianbet": {"platform": "playwright_spa", "url": "https://meridianbet.co.tz/en/betting/football", "keywords": ["betsapi", "events", "standard", "v2", "api", "games", "sports", "football"], "parser": "meridianbet"},
    "parimatch": {"platform": "playwright_spa", "url": "https://parimatch.co.tz/en/football", "keywords": ["prematch", "events", "apg", "sportsbook", "api"], "parser": "generic"},
    "betway": {"platform": "playwright_spa", "url": "https://www.betway.co.tz/sport/soccer", "keywords": ["highlights", "betbook", "sportsapi", "event", "api", "soccer"], "parser": "generic"},
    "sokabet": {"platform": "playwright_spa", "url": "https://sokabet.co.tz", "keywords": ["gettopevents", "events", "sportsbook", "api"], "parser": "generic"},
    "888bet": {"platform": "playwright_spa", "url": "https://888bet.tz/en/sports/football", "keywords": ["sportsbook", "league-card", "highlights", "api"], "parser": "generic"},
    "1win": {"platform": "playwright_spa", "url": "https://1win.pro/bets/home", "keywords": ["events", "line", "api"], "parser": "generic"},
    "wasafibet": {"platform": "playwright_spa", "url": "https://wasafibet.com", "keywords": ["sportsbook", "matches", "api"], "parser": "generic"},
    "kingbet": {"platform": "playwright_spa", "url": "https://kingbet.co.tz", "keywords": ["events", "redis_data", "api"], "parser": "generic"},
    "thronebet": {"platform": "playwright_spa", "url": "https://thronebet.com", "keywords": ["multi", "v2", "api"], "parser": "generic"},
    "pmbet": {"platform": "playwright_spa", "url": "https://pmbet.co.tz/en/sports", "keywords": ["events", "prematch", "api"], "parser": "generic"},
    "bwin": {"platform": "playwright_spa", "url": "https://sports.bwin.com/en/sports/football-4", "keywords": ["cds-api", "events", "api"], "parser": "generic"},
    "unibet": {"platform": "playwright_spa", "url": "https://www.unibet.com/betting/sports/filter/football", "keywords": ["offering", "v2", "api"], "parser": "generic"},
    "bet365": {"platform": "playwright_spa", "url": "https://www.bet365.com", "keywords": ["sportsbook", "api"], "parser": "generic"},
    "stake": {"platform": "playwright_spa", "url": "https://stake.com/sports/soccer", "keywords": ["graphql", "api"], "parser": "generic"},
    "bcgame": {"platform": "playwright_spa", "url": "https://bc.game/sports", "keywords": ["api", "events"], "parser": "generic"},
    "sportsbetio": {"platform": "playwright_spa", "url": "https://sportsbet.io/sports/soccer", "keywords": ["sportsbook", "api"], "parser": "generic"},
    "vbet": {"platform": "playwright_spa", "url": "https://www.vbet.com/sports/football", "keywords": ["events", "api"], "parser": "generic"},
}

BOOKMAKER_MAP = {bm: None for bm in BOOKMAKER_REGISTRY.keys()}


# -------------------------------------------------------------------
# PAYLOAD FINGERPRINTING & PARSER ENGINE
# -------------------------------------------------------------------

def auto_detect_parser(payload: Any) -> str:
    if isinstance(payload, dict):
        if "Value" in payload or ("O1" in str(payload) and "LE" in str(payload)):
            return "1xcorp"
        if "home_team" in str(payload) and "home_odd" in str(payload):
            return "betika"
        if "tournaments" in payload.get("data", {}):
            return "sportybet"
        if "groupList" in payload.get("data", {}) or "matchVoList" in payload.get("data", {}):
            return "bangbet"
        if "events" in payload and ("competitors" in str(payload) or "runners" in str(payload)):
            return "leonbet"
        if "categories" in payload.get("data", {}):
            return "premierbet"
        if "games" in str(payload) and ("leagueName" in str(payload) or "sportId" in str(payload)):
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
        # 1. 1XCORP CLONES (HANDLES BOTH ARRAY & DICT STRUCTURES)
        if parser_type == "1xcorp":
            val = payload.get("Value", []) if isinstance(payload, dict) else []
            if isinstance(val, dict):
                events = val.get("Events") or val.get("G") or val.get("Games") or val.get("E") or []
            elif isinstance(val, list):
                events = val
            else:
                events = []

            for item in events:
                if isinstance(item, dict):
                    home = item.get("O1") or item.get("HT") or item.get("HomeTeam")
                    away = item.get("O2") or item.get("AT") or item.get("AwayTeam")
                    o1, oX, o2 = 1.0, 1.0, 1.0
                    for outcome in item.get("E", []):
                        t = outcome.get("T")
                        if t == 1: o1 = safe_float(outcome.get("C"))
                        elif t == 2: oX = safe_float(outcome.get("C"))
                        elif t == 3: o2 = safe_float(outcome.get("C"))

                    raw_parsed.append({
                        "match_id": str(item.get("I") or item.get("ID") or ""),
                        "home_team": str(home or "").strip(),
                        "away_team": str(away or "").strip(),
                        "competition": str(item.get("LE") or item.get("League") or "Soccer"),
                        "home_odds": o1, "draw_odds": oX, "away_odds": o2,
                        "sport": "soccer", "market_type": "1X2",
                        "bookmaker_id": bookmaker_id, "timestamp": ts, "latency_ms": latency_ms
                    })

        # 2. BETIKA
        elif parser_type == "betika":
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

        # 3. LEONBET
        elif parser_type == "leonbet":
            events = payload.get("events", []) if isinstance(payload, dict) else []
            for item in events:
                if isinstance(item, dict):
                    home, away = None, None
                    competitors = item.get("competitors", [])
                    if len(competitors) >= 2:
                        home = competitors[0].get("name")
                        away = competitors[1].get("name")
                    else:
                        name = item.get("name") or item.get("nameDefault") or ""
                        if " - " in name:
                            parts = name.split(" - ", 1)
                            home, away = parts[0], parts[1]

                    if not home: home = item.get("homeTeam", {}).get("name") or item.get("home")
                    if not away: away = item.get("awayTeam", {}).get("name") or item.get("away")

                    o1, oX, o2 = 1.0, 1.0, 1.0
                    for market in item.get("markets", []):
                        m_type = str(market.get("type", "")).upper()
                        if "1X2" in m_type or "MATCH_RESULT" in m_type or "WINNER" in m_type:
                            for runner in market.get("runners", []):
                                r_type = str(runner.get("type") or runner.get("name", "")).upper()
                                price = safe_float(runner.get("price") or runner.get("odd"))
                                if r_type in ["1", "HOME"]: o1 = price
                                elif r_type in ["X", "DRAW"]: oX = price
                                elif r_type in ["2", "AWAY"]: o2 = price

                    if home and away:
                        raw_parsed.append({
                            "match_id": str(item.get("id") or ""),
                            "home_team": str(home).strip(),
                            "away_team": str(away).strip(),
                            "competition": str(item.get("league", {}).get("name") or "Soccer"),
                            "home_odds": o1, "draw_odds": oX, "away_odds": o2,
                            "sport": "soccer", "market_type": "1X2",
                            "bookmaker_id": bookmaker_id, "timestamp": ts, "latency_ms": latency_ms
                        })

        # 4. PREMIERBET
        elif parser_type == "premierbet":
            categories = payload.get("data", {}).get("categories", []) if isinstance(payload, dict) else []
            for cat in categories:
                for comp in cat.get("competitions", []):
                    comp_name = comp.get("name") or "Soccer"
                    for event in comp.get("events", []):
                        event_names = event.get("eventNames", [])
                        home, away = "", ""
                        if isinstance(event_names, list) and len(event_names) >= 2:
                            home, away = event_names[0], event_names[1]
                        elif " - " in str(event.get("name", "")):
                            parts = event["name"].split(" - ", 1)
                            home, away = parts[0], parts[1]

                        o1, oX, o2 = 1.0, 1.0, 1.0
                        markets = event.get("markets") or []
                        if not markets and "marketGroups" in event:
                            for mg in event.get("marketGroups", []):
                                markets.extend(mg.get("markets", []))

                        for market in markets:
                            selections = market.get("selections") or market.get("outcomes") or market.get("betOffers") or []
                            for idx, sel in enumerate(selections):
                                sel_name = str(sel.get("name") or sel.get("type") or sel.get("outcomeName") or "").upper()
                                price = safe_float(sel.get("price") or sel.get("odds") or sel.get("odd") or sel.get("value"))
                                
                                if sel_name in ["1", "HOME"] or (home and home.upper() in sel_name) or idx == 0:
                                    if o1 == 1.0: o1 = price
                                elif sel_name in ["X", "DRAW"] or idx == 1:
                                    if oX == 1.0: oX = price
                                elif sel_name in ["2", "AWAY"] or (away and away.upper() in sel_name) or idx == 2:
                                    if o2 == 1.0: o2 = price

                        raw_parsed.append({
                            "match_id": str(event.get("id") or ""),
                            "home_team": str(home).strip(),
                            "away_team": str(away).strip(),
                            "competition": str(comp_name),
                            "home_odds": o1, "draw_odds": oX, "away_odds": o2,
                            "sport": "soccer", "market_type": "1X2",
                            "bookmaker_id": bookmaker_id, "timestamp": ts, "latency_ms": latency_ms
                        })

        # 5. BANGBET
        elif parser_type == "bangbet":
            groups = payload.get("data", {}).get("groupList", []) if isinstance(payload, dict) else []
            for group in groups:
                match_list = group.get("matchVoList") or group.get("matchList") or []
                for match in match_list:
                    name = str(match.get("name") or "")
                    home, away = "", ""
                    if " vs. " in name:
                        parts = name.split(" vs. ", 1)
                        home, away = parts[0], parts[1]
                    elif " - " in name:
                        parts = name.split(" - ", 1)
                        home, away = parts[0], parts[1]
                    else:
                        home = match.get("homeName") or match.get("homeTeam") or ""
                        away = match.get("awayName") or match.get("awayTeam") or ""

                    o1, oX, o2 = 1.0, 1.0, 1.0
                    for market in match.get("marketList", []):
                        options = market.get("optionList") or market.get("options") or []
                        for idx, option in enumerate(options):
                            opt_type = str(option.get("type") or option.get("optionType") or option.get("name") or "").upper()
                            price = safe_float(option.get("odds") or option.get("price") or option.get("val") or option.get("odd"))
                            
                            if opt_type in ["1", "HOME"] or idx == 0:
                                if o1 == 1.0: o1 = price
                            elif opt_type in ["X", "DRAW"] or idx == 1:
                                if oX == 1.0: oX = price
                            elif opt_type in ["2", "AWAY"] or idx == 2:
                                if o2 == 1.0: o2 = price

                    raw_parsed.append({
                        "match_id": str(match.get("id") or match.get("matchId") or ""),
                        "home_team": str(home).strip(),
                        "away_team": str(away).strip(),
                        "competition": str(match.get("leagueName") or "Soccer"),
                        "home_odds": o1, "draw_odds": oX, "away_odds": o2,
                        "sport": "soccer", "market_type": "1X2",
                        "bookmaker_id": bookmaker_id, "timestamp": ts, "latency_ms": latency_ms
                    })

        # 6. SPORTPESA
        elif parser_type == "sportpesa":
            games = payload if isinstance(payload, list) else (payload.get("data") or payload.get("games") or payload.get("events") or [])
            for item in games:
                if isinstance(item, dict):
                    home = item.get("homeTeam", {}).get("name") if isinstance(item.get("homeTeam"), dict) else (item.get("homeTeam") or item.get("home_team"))
                    away = item.get("awayTeam", {}).get("name") if isinstance(item.get("awayTeam"), dict) else (item.get("awayTeam") or item.get("away_team"))
                    
                    o1, oX, o2 = 1.0, 1.0, 1.0
                    markets = item.get("markets") or item.get("marketsList") or []
                    for market in markets:
                        m_id = str(market.get("id") or "")
                        m_name = str(market.get("name", "")).upper()
                        if m_id in ["10", "1"] or "1X2" in m_name or "3-WAY" in m_name:
                            for sel in market.get("selections", []):
                                sel_type = str(sel.get("type") or sel.get("name", "")).upper()
                                price = safe_float(sel.get("odds") or sel.get("price"))
                                if sel_type in ["1", "HOME"]: o1 = price
                                elif sel_type in ["X", "DRAW"]: oX = price
                                elif sel_type in ["2", "AWAY"]: o2 = price

                    raw_parsed.append({
                        "match_id": str(item.get("gameId") or item.get("id") or ""),
                        "home_team": str(home or "").strip(),
                        "away_team": str(away or "").strip(),
                        "competition": str(item.get("competition", {}).get("name") if isinstance(item.get("competition"), dict) else (item.get("competition") or "Soccer")),
                        "home_odds": o1, "draw_odds": oX, "away_odds": o2,
                        "sport": "soccer", "market_type": "1X2",
                        "bookmaker_id": bookmaker_id, "timestamp": ts, "latency_ms": latency_ms
                    })

        # 7. MERIDIANBET
        elif parser_type == "meridianbet":
            events = payload.get("events", []) if isinstance(payload, dict) else (payload if isinstance(payload, list) else [])
            for item in events:
                if isinstance(item, dict):
                    home = item.get("home") or item.get("homeTeam")
                    away = item.get("away") or item.get("awayTeam")
                    o1, oX, o2 = 1.0, 1.0, 1.0

                    for market in item.get("markets", []):
                        if market.get("code") in ["1X2", "FINAL_RESULT"] or "1X2" in str(market.get("name", "")).upper():
                            for sel in market.get("selections", []):
                                sel_type = str(sel.get("type") or sel.get("name", "")).upper()
                                price = safe_float(sel.get("price") or sel.get("odds"))
                                if sel_type in ["1", "HOME"]: o1 = price
                                elif sel_type in ["X", "DRAW"]: oX = price
                                elif sel_type in ["2", "AWAY"]: o2 = price

                    raw_parsed.append({
                        "match_id": str(item.get("id") or item.get("eventId") or ""),
                        "home_team": str(home or ""), "away_team": str(away or ""),
                        "competition": str(item.get("leagueName") or item.get("competition") or "Soccer"),
                        "home_odds": o1, "draw_odds": oX, "away_odds": o2,
                        "sport": "soccer", "market_type": "1X2",
                        "bookmaker_id": bookmaker_id, "timestamp": ts, "latency_ms": latency_ms
                    })

        # 8. GENERIC FALLBACK
        else:
            if isinstance(payload, list):
                events = payload
            elif isinstance(payload, dict):
                events = (
                    payload.get("data")
                    or payload.get("events")
                    or payload.get("matches")
                    or [payload]
                )
            else:
                events = []

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
                        "home_team": str(home or "").strip(),
                        "away_team": str(away or "").strip(),
                        "competition": str(item.get("league") or item.get("competition") or "Soccer"),
                        "home_odds": o1, "draw_odds": oX, "away_odds": o2,
                        "sport": "soccer", "market_type": "1X2",
                        "bookmaker_id": bookmaker_id, "timestamp": ts, "latency_ms": latency_ms
                    })

        matches = [m for m in raw_parsed if validate_match(m)]

        if len(matches) == 0 and len(raw_parsed) > 0:
            bm_label = str(bookmaker_id).upper()
            logger.warning(f"[{bm_label}-VALIDATION-REJECT] Extracted {len(raw_parsed)} items, but 0 passed validate_match(). Sample: {raw_parsed[:1]}")

    except Exception as e:
        logger.error(f"[{bookmaker_id}] Parser Exception ({type(e).__name__}): {repr(e)}")

    return matches


# -------------------------------------------------------------------
# CONCURRENT NETWORK FETCHERS WITH RETRIES & SAFE SPORTPESA 2-STEP
# -------------------------------------------------------------------

async def fetch_http_api(session: AsyncSession, bookmaker_id: str, config: dict, retries: int = 3) -> List[Dict[str, Any]]:
    url = config["url"]
    async with HTTP_SEMAPHORE:
        start_t = time.time()
        for attempt in range(retries):
            try:
                if bookmaker_id == "sportpesa":
                    res_init = await session.get(url, headers=REAL_BROWSER_HEADERS, impersonate="chrome", timeout=10)
                    if res_init.status_code == 200:
                        try:
                            data_init = res_init.json()
                            games_list = data_init if isinstance(data_init, list) else (data_init.get("data") or data_init.get("games") or [])
                            game_ids = [str(g.get("id") or g.get("gameId")) for g in games_list if isinstance(g, dict) and (g.get("id") or g.get("gameId"))][:30]
                            if game_ids:
                                markets_url = f"https://www.sportpesa.co.tz/api/games/markets?games={','.join(game_ids)}&markets=10"
                                response = await session.get(markets_url, headers=REAL_BROWSER_HEADERS, impersonate="chrome", timeout=10)
                            else:
                                return []
                        except Exception:
                            return []
                    else:
                        logger.warning(f"[SPORTPESA] Highlights endpoint status {res_init.status_code}")
                        return []
                else:
                    response = await session.get(url, headers=REAL_BROWSER_HEADERS, impersonate="chrome", timeout=15)

                latency_ms = int((time.time() - start_t) * 1000)
                if response.status_code in [200, 203]:
                    data = response.json()
                    matches = parse_raw_payload(bookmaker_id, data, latency_ms=latency_ms)
                    logger.info(f"[{bookmaker_id.upper()}] Parsed {len(matches)} valid matches in {latency_ms}ms.")
                    return matches
                else:
                    logger.warning(f"[{bookmaker_id}] HTTP Status {response.status_code} (Attempt {attempt + 1}/{retries})")
            except Exception as e:
                if attempt == retries - 1:
                    logger.error(f"[{bookmaker_id}] Fetch Error ({type(e).__name__}): {repr(e)}")
                await asyncio.sleep(1.0 * (attempt + 1))
    return []


# -------------------------------------------------------------------
# PLAYWRIGHT INTERCEPTOR WITH TEXT/JSON DECODING & TIMEOUTS
# -------------------------------------------------------------------

async def intercept_playwright_spa(browser: Browser, bookmaker_id: str, config: dict, max_timeout: float = 15.0) -> List[Dict[str, Any]]:
    url = config["url"]
    keywords = config.get("keywords", [])
    bm_label = bookmaker_id.upper()
    captured_payloads = []
    all_matches = []
    
    payload_event = asyncio.Event()

    async with PLAYWRIGHT_SEMAPHORE:
        start_t = time.time()
        try:
            context = await browser.new_context(
                user_agent=REAL_BROWSER_HEADERS["User-Agent"],
                locale="en-US",
                timezone_id="Africa/Dar_es_Salaam",
                viewport={"width": 1280, "height": 720}
            )
            page = await context.new_page()

            async def block_unnecessary_resources(route):
                if route.request.resource_type in ["image", "font", "media"]:
                    await route.abort()
                else:
                    await route.continue_()

            await page.route("**/*", block_unnecessary_resources)

            async def handle_response(response):
                if response.status == 200:
                    res_url = response.url.lower()
                    
                    if bookmaker_id in ["1xbet", "meridianbet"]:
                        if any(ext in res_url for ext in ["/api/", "/bff-api/", "/line/", "get", "events", "feed"]):
                            logger.info(f"[{bm_label}-NETWORK-DISCOVERY] Intercepted URL: {res_url}")

                    if "growthbook" not in res_url and "features" not in res_url and "analytics" not in res_url:
                        if any(kw.lower() in res_url for kw in keywords):
                            json_data = None
                            try:
                                json_data = await response.json()
                            except Exception:
                                try:
                                    raw_text = await response.text()
                                    json_data = json.loads(raw_text)
                                except Exception:
                                    pass

                            if json_data:
                                captured_payloads.append((response.url, json_data))
                                payload_event.set()

            page.on("response", handle_response)
            logger.info(f"[{bm_label}-INTERCEPTOR] Navigating to {url}...")
            
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=20000)
                await page.evaluate("window.scrollBy(0, 500)")
                await asyncio.wait_for(payload_event.wait(), timeout=max_timeout)
            except asyncio.TimeoutError:
                pass
            except Exception as nav_err:
                logger.warning(f"[{bm_label}-INTERCEPTOR] Navigation issue: {type(nav_err).__name__}")

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

    # 1. Fast Parallel HTTP Scrapers
    http_targets = {bm: cfg for bm, cfg in BOOKMAKER_REGISTRY.items() if cfg["platform"] in ["public_rest"]}
    
    async with AsyncSession() as session:
        http_tasks = [fetch_http_api(session, bm, cfg) for bm, cfg in http_targets.items()]
        http_results = await asyncio.gather(*http_tasks, return_exceptions=True)
        for res in http_results:
            if isinstance(res, list):
                all_matches.extend(res)

    # 2. Concurrent Playwright Interceptors
    playwright_targets = {bm: cfg for bm, cfg in BOOKMAKER_REGISTRY.items() if cfg["platform"] == "playwright_spa"}

    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--disable-dev-shm-usage",
                    "--no-sandbox"
                ]
            )
            pw_tasks = [intercept_playwright_spa(browser, bm, cfg) for bm, cfg in playwright_targets.items()]
            pw_results = await asyncio.gather(*pw_tasks, return_exceptions=True)
            for res in pw_results:
                if isinstance(res, list):
                    all_matches.extend(res)
            await browser.close()
    except Exception as e:
        logger.error(f"Playwright Execution Batch Error: {repr(e)}")

    logger.info(f"=== SCAN COMPLETED: Total {len(all_matches)} valid match odds extracted across 33 sportsbooks ===")
    return all_matches
