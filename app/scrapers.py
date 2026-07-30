import asyncio
import logging
import time
import json
from typing import Dict, List, Any, Optional

from curl_cffi.requests import AsyncSession

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("scrapers")

try:
    # patchright is a drop-in Playwright fork purpose-built to defeat
    # automation fingerprinting that plain Playwright + init-script stealth
    # could not get past (confirmed: 0 matches across every SPA target even
    # after adding navigator.webdriver spoofing etc.). Falls back to regular
    # playwright if the package isn't installed yet, so this doesn't break
    # deploys — but add `patchright` to requirements.txt and run
    # `patchright install chromium` in your build step to actually use it.
    from patchright.async_api import async_playwright, Browser
    logger.info("Using patchright for browser automation (stealth fork).")
except ImportError:
    from playwright.async_api import async_playwright, Browser
    logger.warning("patchright not installed — falling back to plain playwright (add 'patchright' to requirements.txt).")

# Modern Chrome Browser Headers to pass anti-bot header checks
# NOTE: Removed fake Origin/Referer pointing at Google — a real page calling its
# own /api/ endpoint never sends Origin: google.com. That mismatch is itself
# a bot-detection signal. Sec-Fetch-Site changed to same-origin to match.
REAL_BROWSER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Sec-Fetch-Site": "same-origin",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Dest": "empty",
    "Sec-Ch-Ua": '"Not/A)Brand";v="8", "Chromium";v="126", "Google Chrome";v="126"',
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"Windows"',
}

# Concurrency tuning specifically optimized for Render Free Tier (512MB RAM / 1 vCPU)
HTTP_SEMAPHORE = asyncio.Semaphore(12)
PLAYWRIGHT_SEMAPHORE = asyncio.Semaphore(2)  # Strict semaphore = 2 to prevent RAM OOM crashes


def safe_float(val: Any, default: Optional[float] = None) -> Optional[float]:
    """Hardened float conversion that returns default (None) for invalid/negative values."""
    if val is None:
        return default
    try:
        parsed = float(val)
        return parsed if parsed > 1.01 else default
    except (ValueError, TypeError):
        return default


def extract_team_name(val: Any) -> str:
    """Prevents raw dict stringification (dict bleed) in team name fields."""
    if isinstance(val, dict):
        return str(val.get("name") or val.get("nameDefault") or val.get("title") or "").strip()
    return str(val or "").strip()


def validate_match(match: Dict[str, Any]) -> bool:
    """
    Market-aware validation requiring clean team names and at least 2 valid odds choices (> 1.01).
    Allows 2-way and lazy-loaded draw markets without dropping valid event pairs.
    """
    home = match.get("home_team", "")
    away = match.get("away_team", "")

    if len(home) < 2 or len(away) < 2 or home.startswith("{") or away.startswith("{"):
        return False

    o1 = match.get("home_odds")
    oX = match.get("draw_odds")
    o2 = match.get("away_odds")

    valid_odds_count = sum(1 for x in [o1, oX, o2] if x is not None and x > 1.01)

    # Require at least 2 valid outcome odds choices
    return valid_odds_count >= 2


# -------------------------------------------------------------------
# BOOKMAKER REGISTRY
# -------------------------------------------------------------------

BOOKMAKER_REGISTRY = {
    # Tier 1: Fast REST APIs (TLS Impersonation)
    "betika": {"platform": "public_rest", "url": "https://api.betika.com/v1/uo/matches?limit=100&sub_type=prematch", "parser": "betika"},
    "sportybet": {"platform": "public_rest", "url": "https://www.sportybet.com/api/tz/factsCenter/pcUpcomingEvents?sportId=sr%3Asport%3A1&marketId=1%2C18%2C10%2C29%2C11%2C26%2C36%2C14%2C60100&pageSize=100&pageNum=1&option=1", "parser": "sportybet"},
    "bangbet": {"platform": "public_rest", "url": "https://bet-api.bangbet.com/api/bet/match/listTop?country=tz", "parser": "bangbet"},
    "sportpesa": {"platform": "public_rest", "url": "https://www.sportpesa.co.tz/api/games/markets?markets=10", "parser": "sportpesa"},
    "leonbet": {"platform": "public_rest", "url": "https://leonbet.co.tz/api-2/betline/events/all?ctag=en-US", "parser": "leonbet"},
    "premierbet": {"platform": "public_rest", "url": "https://sports-api.premierbet.co.tz/v1/events/highlights?country=TZ&group=g2&platform=desktop&locale=en&sportId=1&limit=50", "parser": "premierbet"},
    "galsport": {"platform": "public_rest", "url": "https://gsb.co.tz/api/v1/sportsbook/highlights?sportId=1", "parser": "generic"},

    # Tier 2: Protected / Intercepted Platforms (Broad Interception Keywords)
    "meridianbet": {"platform": "playwright_spa", "url": "https://meridianbet.co.tz/en/betting/football", "keywords": ["/api/", "/events/", "betsapi", "standard", "v2"], "parser": "meridianbet"},
    "1xbet": {"platform": "playwright_spa", "url": "https://1xbet.co.tz/en/line/football", "keywords": ["/linefeed/", "/livefeed/", "/bff-api/", "getclubslinezip", "Zip", "get1x2", "/line/"], "parser": "1xcorp"},
    "22bet": {"platform": "playwright_spa", "url": "https://22bet.co.tz/en/line/football", "keywords": ["/linefeed/", "/livefeed/", "/bff-api/", "getclubslinezip", "Zip", "get1x2", "/line/"], "parser": "1xcorp"},
    "helabet": {"platform": "playwright_spa", "url": "https://helabet.co.tz/en/line/football", "keywords": ["/linefeed/", "/livefeed/", "/bff-api/", "getclubslinezip", "Zip", "get1x2", "/line/"], "parser": "1xcorp"},
    "betwinner": {"platform": "playwright_spa", "url": "https://betwinner.co.tz/en/line/football", "keywords": ["/linefeed/", "/livefeed/", "/bff-api/", "getclubslinezip", "Zip", "get1x2", "/line/"], "parser": "1xcorp"},
    "melbet": {"platform": "playwright_spa", "url": "https://melbet.co.tz/en/line/football", "keywords": ["/linefeed/", "/livefeed/", "/bff-api/", "getclubslinezip", "Zip", "get1x2", "/line/"], "parser": "1xcorp"},
    "1xbit": {"platform": "playwright_spa", "url": "https://1xbit.com/en/line/football", "keywords": ["/linefeed/", "/livefeed/", "/bff-api/", "getclubslinezip", "Zip", "get1x2", "/line/"], "parser": "1xcorp"},

    "parimatch": {"platform": "playwright_spa", "url": "https://parimatch.co.tz/en/football", "keywords": ["prematch", "events", "apg", "sportsbook", "/api/"], "parser": "generic"},
    "betway": {"platform": "playwright_spa", "url": "https://www.betway.co.tz/sport/soccer", "keywords": ["highlights", "betbook", "sportsapi", "event", "/api/"], "parser": "generic"},
    "sokabet": {"platform": "playwright_spa", "url": "https://sokabet.co.tz", "keywords": ["gettopevents", "events", "sportsbook", "/api/"], "parser": "generic"},
    "888bet": {"platform": "playwright_spa", "url": "https://888bet.tz/en/sports/football", "keywords": ["sportsbook", "league-card", "highlights", "/api/"], "parser": "generic"},
    "wasafibet": {"platform": "playwright_spa", "url": "https://wasafibet.com", "keywords": ["sportsbook", "matches", "/api/"], "parser": "generic"},
    "kingbet": {"platform": "playwright_spa", "url": "https://kingbet.co.tz", "keywords": ["events", "redis_data", "/api/"], "parser": "generic"},
    "pmbet": {"platform": "playwright_spa", "url": "https://pmbet.co.tz/en/sports", "keywords": ["events", "prematch", "/api/"], "parser": "generic"},
}

BOOKMAKER_MAP = {bm: None for bm in BOOKMAKER_REGISTRY.keys()}


# -------------------------------------------------------------------
# FAST STRUCTURAL PAYLOAD FINGERPRINTING ENGINE
# -------------------------------------------------------------------

def auto_detect_parser(payload: Any) -> str:
    """Structural payload fingerprinting without str(payload) memory bloat."""
    if isinstance(payload, dict):
        if "Value" in payload or "LE" in payload:
            return "1xcorp"
        if "home_team" in payload or "home_odd" in payload:
            return "betika"
        data_obj = payload.get("data")
        if isinstance(data_obj, dict):
            if "tournaments" in data_obj:
                return "sportybet"
            if "groupList" in data_obj or "matchVoList" in data_obj:
                return "bangbet"
            if "categories" in data_obj:
                return "premierbet"
        if "events" in payload:
            return "leonbet"
        if "games" in payload:
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
        # 1. 1XCORP CLONES (ARRAY + DICT NORMALIZATION)
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
                    home = extract_team_name(item.get("O1") or item.get("HT") or item.get("HomeTeam"))
                    away = extract_team_name(item.get("O2") or item.get("AT") or item.get("AwayTeam"))
                    o1, oX, o2 = None, None, None
                    for outcome in item.get("E", []):
                        t = outcome.get("T")
                        if t == 1: o1 = safe_float(outcome.get("C"))
                        elif t == 2: oX = safe_float(outcome.get("C"))
                        elif t == 3: o2 = safe_float(outcome.get("C"))

                    raw_parsed.append({
                        "match_id": str(item.get("I") or item.get("ID") or ""),
                        "home_team": home, "away_team": away,
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
                        "home_team": extract_team_name(item.get("home_team")),
                        "away_team": extract_team_name(item.get("away_team")),
                        "competition": str(item.get("competition_name") or "Soccer"),
                        "home_odds": safe_float(item.get("home_odd")),
                        "draw_odds": safe_float(item.get("neutral_odd")),
                        "away_odds": safe_float(item.get("away_odd")),
                        "sport": "soccer", "market_type": "1X2",
                        "bookmaker_id": bookmaker_id, "timestamp": ts, "latency_ms": latency_ms
                    })

        # 3. LEONBET (ENHANCED RUNNER TRAVERSAL)
        elif parser_type == "leonbet":
            events = payload.get("events", []) if isinstance(payload, dict) else []
            for item in events:
                if isinstance(item, dict):
                    home, away = "", ""
                    competitors = item.get("competitors", [])
                    if len(competitors) >= 2:
                        home = extract_team_name(competitors[0])
                        away = extract_team_name(competitors[1])
                    else:
                        name = item.get("name") or item.get("nameDefault") or ""
                        if " - " in name:
                            parts = name.split(" - ", 1)
                            home, away = parts[0], parts[1]

                    if not home: home = extract_team_name(item.get("homeTeam") or item.get("home"))
                    if not away: away = extract_team_name(item.get("awayTeam") or item.get("away"))

                    o1, oX, o2 = None, None, None
                    for market in item.get("markets", []):
                        m_name = str(market.get("name", "")).upper()
                        m_type = str(market.get("type", "")).upper()

                        if "1X2" in m_name or "WINNER" in m_name or "1X2" in m_type or "MATCH_RESULT" in m_type or market.get("primary") is True:
                            for runner in market.get("runners", []):
                                price = safe_float(runner.get("price") or runner.get("priceStr") or runner.get("odd"))
                                r_type = str(runner.get("type") or runner.get("name") or "").upper()
                                tags = [str(t).upper() for t in runner.get("tags", [])]

                                if "HOME" in tags or r_type in ["1", "HOME"] or (home and home.upper() in r_type):
                                    if o1 is None: o1 = price
                                elif "DRAW" in tags or r_type in ["X", "DRAW"]:
                                    if oX is None: oX = price
                                elif "AWAY" in tags or r_type in ["2", "AWAY"] or (away and away.upper() in r_type):
                                    if o2 is None: o2 = price

                    if home and away:
                        raw_parsed.append({
                            "match_id": str(item.get("id") or ""),
                            "home_team": home, "away_team": away,
                            "competition": str(item.get("league", {}).get("name") or item.get("family", {}).get("name") or "Soccer"),
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
                            home, away = extract_team_name(event_names[0]), extract_team_name(event_names[1])
                        elif " - " in str(event.get("name", "")):
                            parts = event["name"].split(" - ", 1)
                            home, away = parts[0], parts[1]

                        o1, oX, o2 = None, None, None
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
                                    if o1 is None: o1 = price
                                elif sel_name in ["X", "DRAW"] or idx == 1:
                                    if oX is None: oX = price
                                elif sel_name in ["2", "AWAY"] or (away and away.upper() in sel_name) or idx == 2:
                                    if o2 is None: o2 = price

                        raw_parsed.append({
                            "match_id": str(event.get("id") or ""),
                            "home_team": home, "away_team": away,
                            "competition": str(comp_name),
                            "home_odds": o1, "draw_odds": oX, "away_odds": o2,
                            "sport": "soccer", "market_type": "1X2",
                            "bookmaker_id": bookmaker_id, "timestamp": ts, "latency_ms": latency_ms
                        })

        # 5. BANGBET
        # Real schema (confirmed via prior debug dump): each match has its own
        # homeTeamName/awayTeamName fields directly, and marketList is a list of
        # market GROUPS, each of which nests an inner "markets" list. The actual
        # odds live in market["outcomes"], where each outcome has a "desc" field
        # (either the team name text, or literally "draw") and an "odds" field —
        # there is no type/HOME/AWAY marker, so we match on desc text + position.
        elif parser_type == "bangbet":
            groups = payload.get("data", {}).get("groupList", []) if isinstance(payload, dict) else []
            for group in groups:
                match_list = group.get("matchVoList") or group.get("matchList") or []

                for match in match_list:
                    home = extract_team_name(match.get("homeTeamName") or match.get("homeName") or match.get("homeTeam"))
                    away = extract_team_name(match.get("awayTeamName") or match.get("awayName") or match.get("awayTeam"))

                    if not home or not away:
                        name = str(match.get("name") or "")
                        if " vs. " in name:
                            parts = name.split(" vs. ", 1)
                            home, away = home or parts[0], away or parts[1]
                        elif " - " in name:
                            parts = name.split(" - ", 1)
                            home, away = home or parts[0], away or parts[1]

                    o1, oX, o2 = None, None, None

                    for market_group in match.get("marketList", []):
                        # Bangbet nests real markets one level deeper under "markets".
                        # Fall back to treating the group itself as the market if
                        # a variant payload skips that extra nesting.
                        inner_markets = market_group.get("markets") or (
                            [market_group] if market_group.get("outcomes") else []
                        )

                        for market in inner_markets:
                            m_name = str(market.get("name") or market.get("marketName") or "").upper()
                            if not ("1X2" in m_name or "3-WAY" in m_name or str(market.get("id")) == "1"):
                                continue

                            outcomes = market.get("outcomes") or market.get("optionList") or market.get("options") or []
                            for idx, outcome in enumerate(outcomes):
                                desc = str(
                                    outcome.get("desc") or outcome.get("type")
                                    or outcome.get("optionType") or outcome.get("name") or ""
                                ).strip()
                                desc_upper = desc.upper()
                                raw_price = outcome.get("odds") or outcome.get("price") or outcome.get("val")

                                # Safe float parsing before division
                                try:
                                    raw_val = float(raw_price) if raw_price is not None else None
                                except (ValueError, TypeError):
                                    raw_val = None

                                if raw_val is not None and raw_val >= 100:
                                    price = safe_float(raw_val / 1000.0)
                                else:
                                    price = safe_float(raw_val)

                                if desc_upper in ["DRAW", "X"]:
                                    if oX is None: oX = price
                                elif desc_upper in ["1", "HOME"] or (home and home.upper() == desc_upper) or idx == 0:
                                    if o1 is None: o1 = price
                                elif desc_upper in ["2", "AWAY"] or (away and away.upper() == desc_upper) or idx == 2:
                                    if o2 is None: o2 = price

                    raw_parsed.append({
                        "match_id": str(match.get("id") or match.get("matchId") or ""),
                        "home_team": home, "away_team": away,
                        "competition": str(match.get("tournamentName") or match.get("leagueName") or "Soccer"),
                        "home_odds": o1, "draw_odds": oX, "away_odds": o2,
                        "sport": "soccer", "market_type": "1X2",
                        "bookmaker_id": bookmaker_id, "timestamp": ts, "latency_ms": latency_ms
                    })

        # 6. SPORTYBET
        elif parser_type == "sportybet":
            data_obj = payload.get("data", {}) if isinstance(payload, dict) else {}
            tournaments = data_obj.get("tournaments", []) or data_obj.get("events", [])
            for tourney in tournaments:
                events = tourney.get("events", []) if isinstance(tourney, dict) else [tourney]
                for item in events:
                    home = extract_team_name(item.get("homeTeamName") or item.get("homeTeam"))
                    away = extract_team_name(item.get("awayTeamName") or item.get("awayTeam"))
                    o1, oX, o2 = None, None, None
                    for market in item.get("markets", []):
                        if str(market.get("id")) in ["1", "10"] or market.get("name") in ["1X2", "3-Way"]:
                            for outcome in market.get("outcomes", []):
                                desc = str(outcome.get("desc") or outcome.get("outcomeName"))
                                price = safe_float(outcome.get("odds"))
                                if desc in ["1", "Home"]: o1 = price
                                elif desc in ["X", "Draw"]: oX = price
                                elif desc in ["2", "Away"]: o2 = price

                    raw_parsed.append({
                        "match_id": str(item.get("eventId") or item.get("id") or ""),
                        "home_team": home, "away_team": away,
                        "competition": str(tourney.get("name") or "Soccer"),
                        "home_odds": o1, "draw_odds": oX, "away_odds": o2,
                        "sport": "soccer", "market_type": "1X2",
                        "bookmaker_id": bookmaker_id, "timestamp": ts, "latency_ms": latency_ms
                    })

        # 7. SPORTPESA
        elif parser_type == "sportpesa":
            games = payload if isinstance(payload, list) else (payload.get("data") or payload.get("games") or payload.get("events") or [])
            for item in games:
                if isinstance(item, dict):
                    home = extract_team_name(item.get("homeTeam") or item.get("home_team"))
                    away = extract_team_name(item.get("awayTeam") or item.get("away_team"))

                    o1, oX, o2 = None, None, None
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
                        "home_team": home, "away_team": away,
                        "competition": str(item.get("competition", {}).get("name") if isinstance(item.get("competition"), dict) else (item.get("competition") or "Soccer")),
                        "home_odds": o1, "draw_odds": oX, "away_odds": o2,
                        "sport": "soccer", "market_type": "1X2",
                        "bookmaker_id": bookmaker_id, "timestamp": ts, "latency_ms": latency_ms
                    })

        # 8. MERIDIANBET
        elif parser_type == "meridianbet":
            events = payload.get("events", []) if isinstance(payload, dict) else (payload if isinstance(payload, list) else [])
            for item in events:
                if isinstance(item, dict):
                    home = extract_team_name(item.get("home") or item.get("homeTeam"))
                    away = extract_team_name(item.get("away") or item.get("awayTeam"))
                    o1, oX, o2 = None, None, None

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
                        "home_team": home, "away_team": away,
                        "competition": str(item.get("leagueName") or item.get("competition") or "Soccer"),
                        "home_odds": o1, "draw_odds": oX, "away_odds": o2,
                        "sport": "soccer", "market_type": "1X2",
                        "bookmaker_id": bookmaker_id, "timestamp": ts, "latency_ms": latency_ms
                    })

        # 9. GENERIC FALLBACK
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
                    home = extract_team_name(item.get("homeTeam") or item.get("home_team") or item.get("homeName") or item.get("team1"))
                    away = extract_team_name(item.get("awayTeam") or item.get("away_team") or item.get("awayName") or item.get("team2"))
                    raw_odds = item.get("odds") or {}
                    o1 = safe_float(item.get("home_odds") or item.get("homeOdds") or item.get("odds1") or raw_odds.get("1"))
                    oX = safe_float(item.get("draw_odds") or item.get("drawOdds") or item.get("oddsX") or raw_odds.get("X"))
                    o2 = safe_float(item.get("away_odds") or item.get("awayOdds") or item.get("odds2") or raw_odds.get("2"))

                    raw_parsed.append({
                        "match_id": str(item.get("id") or item.get("eventId") or item.get("match_id") or ""),
                        "home_team": home, "away_team": away,
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
# CONCURRENT NETWORK FETCHERS WITH OUTER TASK TIMEOUTS
# -------------------------------------------------------------------

async def fetch_http_api(session: AsyncSession, bookmaker_id: str, config: dict, retries: int = 3) -> List[Dict[str, Any]]:
    url = config["url"]
    async with HTTP_SEMAPHORE:
        start_t = time.time()
        for attempt in range(retries):
            try:
                # Wrap network request inside an outer 18-second timeout guard
                response = await asyncio.wait_for(
                    session.get(url, headers=REAL_BROWSER_HEADERS, impersonate="chrome", timeout=12),
                    timeout=18.0
                )
                latency_ms = int((time.time() - start_t) * 1000)

                if response.status_code in [200, 203]:
                    try:
                        data = response.json()
                    except Exception:
                        # 200 OK but body isn't valid JSON — usually a Cloudflare/JS
                        # challenge page, WAF block page, or an API error wrapped
                        # in HTML. Log a preview instead of failing silently with
                        # just "JSONDecodeError".
                        preview = (
                            response.text[:500]
                            .replace("\n", " ")
                            .replace("\r", " ")
                        )
                        logger.warning(
                            f"[{bookmaker_id}] Got {response.status_code} but non-JSON body "
                            f"(Attempt {attempt + 1}/{retries}): {preview}"
                        )
                        await asyncio.sleep(1.0 * (attempt + 1))
                        continue

                    matches = parse_raw_payload(bookmaker_id, data, latency_ms=latency_ms)
                    logger.info(f"[{bookmaker_id.upper()}] Parsed {len(matches)} valid matches in {latency_ms}ms.")
                    return matches
                else:
                    body_preview = ""
                    try:
                        body_preview = (
                            response.text[:500]
                            .replace("\n", " ")
                            .replace("\r", " ")
                        )
                    except Exception:
                        pass
                    logger.warning(
                        f"[{bookmaker_id}] HTTP Status {response.status_code} "
                        f"(Attempt {attempt + 1}/{retries}) Body: {body_preview}"
                    )
            except Exception as e:
                if attempt == retries - 1:
                    logger.error(f"[{bookmaker_id}] Fetch Error ({type(e).__name__}): {repr(e)}")
                await asyncio.sleep(1.0 * (attempt + 1))
    return []


# -------------------------------------------------------------------
# PLAYWRIGHT INTERCEPTOR WITH MULTI-PAYLOAD COLLECTOR & RAM PROTECTION
# -------------------------------------------------------------------

async def intercept_playwright_spa(browser: Browser, bookmaker_id: str, config: dict, max_timeout: float = 8.0) -> List[Dict[str, Any]]:
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
                locale="en-US",
                timezone_id="Africa/Dar_es_Salaam",
                viewport={"width": 1280, "height": 720},
                extra_http_headers={
                    "Accept-Language": "en-US,en;q=0.9",
                    "Sec-Ch-Ua": REAL_BROWSER_HEADERS["Sec-Ch-Ua"],
                    "Sec-Ch-Ua-Mobile": "?0",
                    "Sec-Ch-Ua-Platform": REAL_BROWSER_HEADERS["Sec-Ch-Ua-Platform"],
                },
            )

            # Stealth init script: neutralize the most common automation
            # fingerprints that anti-bot JS checks for before deciding whether
            # to serve real data. Runs before any site JS on every new page.
            await context.add_init_script("""
                Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
                Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
                Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
                window.chrome = { runtime: {} };
                const originalQuery = window.navigator.permissions.query;
                window.navigator.permissions.query = (parameters) => (
                    parameters.name === 'notifications'
                        ? Promise.resolve({ state: Notification.permission })
                        : originalQuery(parameters)
                );
                Object.defineProperty(navigator, 'hardwareConcurrency', { get: () => 8 });
                Object.defineProperty(navigator, 'deviceMemory', { get: () => 8 });
            """)

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

                            # Content-type safe decoding
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

            page.on("response", handle_response)

            # Diagnostic + capture: some live-odds SPAs push match/odds data
            # over a WebSocket rather than plain HTTP fetch/XHR, which
            # page.on("response") cannot see at all. Log every socket opened
            # and try to capture any JSON frames that match our keywords, so
            # we can tell definitively whether this is the real data channel.
            def handle_websocket(ws):
                logger.info(f"[{bm_label}-WS-DISCOVERY] WebSocket opened: {ws.url}")

                def handle_frame(payload):
                    try:
                        text = payload if isinstance(payload, str) else payload.decode("utf-8", errors="ignore")
                    except Exception:
                        return
                    lowered = text.lower()
                    if any(kw.lower() in lowered for kw in keywords) or "odds" in lowered or "match" in lowered:
                        try:
                            json_data = json.loads(text)
                            captured_payloads.append((ws.url, json_data))
                            logger.info(f"[{bm_label}-WS-CAPTURE] Captured JSON frame from {ws.url} ({len(text)} chars)")
                        except Exception:
                            # Non-JSON frame (binary protocol, ping/pong, etc.) — just note it happened
                            logger.info(f"[{bm_label}-WS-CAPTURE] Non-JSON frame from {ws.url}, preview: {text[:150]}")

                ws.on("framereceived", handle_frame)

            page.on("websocket", handle_websocket)
            logger.info(f"[{bm_label}-INTERCEPTOR] Navigating to {url}...")

            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=10000)

                # Give the SPA a chance to reach network idle (real odds XHRs
                # usually fire during this window) before falling back to a
                # fixed sleep. This is where domcontentloaded alone was letting
                # us capture only early config/feature-flag calls.
                try:
                    await page.wait_for_load_state("networkidle", timeout=6000)
                except Exception:
                    pass

                # Simulate minimal human interaction — some SPAs only fire the
                # real market feed after a scroll or viewport-intersection event.
                await page.mouse.move(300, 400)
                await page.evaluate("window.scrollBy(0, 500)")
                await asyncio.sleep(1.0)
                await page.evaluate("window.scrollBy(0, 800)")

                # Multi-payload collector window: sleep to catch stacked market XHR responses
                await asyncio.sleep(5.0)
            except Exception:
                logger.warning(f"[{bm_label}-INTERCEPTOR] Navigation timeout warning, processing captured payloads...")

            await page.close()
            await context.close()

            latency_ms = int((time.time() - start_t) * 1000)

            for res_url, payload in captured_payloads:
                parsed = parse_raw_payload(bookmaker_id, payload, latency_ms=latency_ms)
                all_matches.extend(parsed)

            # Unique match key incorporates bookmaker_id to prevent 1XCorp clone ID collisions
            unique_matches = list({f"{m['bookmaker_id']}_{m['match_id']}": m for m in all_matches if m.get("match_id")}.values()) if all_matches else []
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

    logger.info(f"=== SCAN COMPLETED: Total {len(all_matches)} valid match odds extracted across registered sportsbooks ===")
    return all_matches
