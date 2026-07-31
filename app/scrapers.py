import asyncio
import logging
import time
import json
from collections import Counter
from typing import Dict, List, Any, Optional
from urllib.parse import urlparse

from curl_cffi.requests import AsyncSession

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("scrapers")

try:
    from patchright.async_api import async_playwright, Browser
    logger.info("Using patchright for browser automation (stealth fork).")
except ImportError:
    from playwright.async_api import async_playwright, Browser
    logger.warning("patchright not installed — falling back to plain playwright.")

DESKTOP_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"

REAL_BROWSER_HEADERS = {
    "User-Agent": DESKTOP_USER_AGENT,
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

SPORT_MAP = {
    "sr:sport:1": "soccer", "1": "soccer",
    "sr:sport:2": "basketball", "2": "basketball",
    "sr:sport:3": "baseball", "3": "baseball",
    "sr:sport:4": "ice_hockey", "4": "ice_hockey",
    "sr:sport:5": "tennis", "5": "tennis",
    "sr:sport:6": "handball", "6": "handball",
    "sr:sport:12": "rugby", "12": "rugby",
    "sr:sport:20": "table_tennis", "20": "table_tennis",
    "sr:sport:21": "cricket", "21": "cricket",
    "sr:sport:22": "darts", "22": "darts",
    "sr:sport:23": "table_tennis", "23": "table_tennis",
    "sr:sport:31": "volleyball", "10": "volleyball",
    "sr:sport:109": "esports",
    "sr:sport:117": "mma", "117": "mma",
}

HTTP_SEMAPHORE = asyncio.Semaphore(16)
PLAYWRIGHT_SEMAPHORE = asyncio.Semaphore(2)


def resolve_sport_name(raw_val: Any) -> str:
    if not raw_val:
        return "soccer"
    val_str = str(raw_val).strip().lower()
    if val_str in SPORT_MAP:
        return SPORT_MAP[val_str]
    
    if any(k in val_str for k in ["foot", "soccer", "football"]):
        return "soccer"
    elif "basket" in val_str:
        return "basketball"
    elif "tennis" in val_str and "table" not in val_str:
        return "tennis"
    elif "table" in val_str or "ping" in val_str:
        return "table_tennis"
    elif "volley" in val_str:
        return "volleyball"
    elif any(k in val_str for k in ["mma", "ufc", "fighting", "boxing"]):
        return "mma"
    elif "handball" in val_str:
        return "handball"
    elif "dart" in val_str:
        return "darts"
    return val_str


def generate_event_fingerprint(home: str, away: str, sport: str) -> str:
    ignored_words = {"fc", "cf", "united", "city", "town", "real", "athletic", "club", "sc", "sporting", "st", "saint"}
    
    def tokenize(name: str) -> List[str]:
        words = [w for w in "".join(c if c.isalnum() else " " for c in name.lower()).split() if len(w) > 1]
        meaningful = [w for w in words if w not in ignored_words]
        return sorted(meaningful) if meaningful else sorted(words)

    home_tokens = tokenize(home)
    away_tokens = tokenize(away)

    if not home_tokens or not away_tokens:
        return f"{sport}_{home.lower()[:8]}_{away.lower()[:8]}"

    home_key = "_".join(home_tokens[:2])
    away_key = "_".join(away_tokens[:2])
    return f"{sport}_{home_key}_vs_{away_key}"


def get_dynamic_headers(target_url: str) -> Dict[str, str]:
    parsed = urlparse(target_url)
    clean_netloc = parsed.netloc.replace("api.", "www.").replace("bet-api.", "www.")
    origin = f"{parsed.scheme}://{clean_netloc}"
    headers = dict(REAL_BROWSER_HEADERS)
    headers["Referer"] = f"{origin}/"
    headers["Origin"] = origin
    return headers


def safe_float(val: Any, default: Optional[float] = None) -> Optional[float]:
    if val is None:
        return default
    try:
        parsed = float(val)
        return parsed if parsed > 1.01 else default
    except (ValueError, TypeError):
        return default


def extract_team_name(val: Any) -> str:
    if isinstance(val, dict):
        return str(val.get("name") or val.get("nameDefault") or val.get("title") or "").strip()
    return str(val or "").strip()


def validate_match(match: Dict[str, Any]) -> bool:
    if not isinstance(match, dict):
        return False

    home = extract_team_name(match.get("home_team", ""))
    away = extract_team_name(match.get("away_team", ""))
    comp = str(match.get("competition", "")).lower()
    
    # Filter virtual/simulated RNG matches
    virtual_keywords = ["zoom", "virtual", "cyber", "simulated", "srl", "esoccer", "eleague"]
    if any(k in comp or k in home.lower() or k in away.lower() for k in virtual_keywords):
        return False

    if len(home) < 2 or len(away) < 2 or home.startswith("{") or away.startswith("{"):
        return False

    o1 = match.get("home_odds")
    oX = match.get("draw_odds")
    o2 = match.get("away_odds")

    valid_odds = [o for o in [o1, oX, o2] if o is not None and o > 1.01]
    sport = resolve_sport_name(match.get("sport", "soccer"))

    if sport in ["tennis", "basketball", "volleyball", "mma", "table_tennis", "baseball", "darts", "handball"]:
        return len(valid_odds) >= 2
    else:
        return len(valid_odds) >= 2 and oX is not None


def find_events_recursive(obj: Any, depth: int = 0) -> List[Dict[str, Any]]:
    if depth > 4:
        return []
    if isinstance(obj, list):
        if len(obj) > 0 and isinstance(obj[0], dict):
            sample = obj[0]
            if any(k in sample for k in ["home_team", "homeTeam", "competitors", "eventNames", "O1", "teams"]):
                return [x for x in obj if isinstance(x, dict)]
        events = []
        for item in obj[:10]:
            if isinstance(item, (dict, list)):
                found = find_events_recursive(item, depth + 1)
                if found:
                    events.extend(found)
        return events
    elif isinstance(obj, dict):
        for key in ["events", "matches", "games", "fixtures", "items", "groupList", "matchVoList", "data"]:
            if key in obj:
                val = obj[key]
                if isinstance(val, list):
                    return [x for x in val if isinstance(x, dict)]
                elif isinstance(val, dict):
                    found = find_events_recursive(val, depth + 1)
                    if found:
                        return found
        for k, v in obj.items():
            if isinstance(v, (dict, list)):
                found = find_events_recursive(v, depth + 1)
                if found:
                    return found
    return []


# -------------------------------------------------------------------
# CROSS-BOOKMAKER ARBITRAGE CALCULATOR
# -------------------------------------------------------------------

def find_arbitrage_opportunities(all_matches: List[Dict[str, Any]], bankroll: float = 100000.0) -> List[Dict[str, Any]]:
    grouped_events: Dict[str, List[Dict[str, Any]]] = {}

    for m in all_matches:
        if not isinstance(m, dict):
            continue

        home = str(m.get("home_team", ""))
        away = str(m.get("away_team", ""))
        sport = str(m.get("sport", "soccer"))

        if not home or not away:
            continue

        key = generate_event_fingerprint(home, away, sport)
        grouped_events.setdefault(key, []).append(m)

    opportunities = []

    for key, matches in grouped_events.items():
        valid_matches = [m for m in matches if isinstance(m, dict)]
        if len(valid_matches) < 2:
            continue

        distinct_bookies = set(str(m.get("bookmaker_id")) for m in valid_matches if m.get("bookmaker_id"))
        if len(distinct_bookies) < 2:
            continue

        sport = valid_matches[0].get("sport", "soccer")
        home_name = valid_matches[0].get("home_team", "")
        away_name = valid_matches[0].get("away_team", "")
        competition = valid_matches[0].get("competition", "Unknown")

        best_home = max(valid_matches, key=lambda x: x.get("home_odds") or 0)
        best_away = max(valid_matches, key=lambda x: x.get("away_odds") or 0)

        o1 = best_home.get("home_odds")
        o2 = best_away.get("away_odds")

        if not o1 or not o2:
            continue

        # 1. 2-WAY ARBITRAGE
        if sport in ["tennis", "basketball", "volleyball", "mma", "table_tennis", "baseball", "darts", "handball"]:
            if str(best_home.get("bookmaker_id")) == str(best_away.get("bookmaker_id")):
                continue

            arb_margin = (1.0 / o1) + (1.0 / o2)

            if 0.85 < arb_margin < 1.0:
                profit_pct = round((1.0 - arb_margin) * 100, 2)

                stake_1 = round(bankroll / (o1 * arb_margin), -1)
                stake_2 = round(bankroll / (o2 * arb_margin), -1)
                total_invested = stake_1 + stake_2

                payout_1 = round(stake_1 * o1, 2)
                payout_2 = round(stake_2 * o2, 2)
                min_payout = min(payout_1, payout_2)
                net_profit = round(min_payout - total_invested, 2)

                opportunities.append({
                    "sport": sport.upper(),
                    "competition": competition,
                    "event": f"{home_name} vs {away_name}",
                    "market_type": "2-Way Moneyline",
                    "profit_margin_pct": profit_pct,
                    "total_investment": total_invested,
                    "guaranteed_net_profit": net_profit,
                    "guaranteed_payout": min_payout,
                    "legs": [
                        {
                            "outcome": f"1 ({home_name})",
                            "bookmaker": str(best_home.get("bookmaker_id", "")).upper(),
                            "odds": o1,
                            "recommended_stake": stake_1,
                            "expected_payout": payout_1
                        },
                        {
                            "outcome": f"2 ({away_name})",
                            "bookmaker": str(best_away.get("bookmaker_id", "")).upper(),
                            "odds": o2,
                            "recommended_stake": stake_2,
                            "expected_payout": payout_2
                        }
                    ]
                })

        # 2. 3-WAY ARBITRAGE
        else:
            best_draw = max([m for m in valid_matches if m.get("draw_odds") is not None], key=lambda x: x.get("draw_odds") or 0, default=None)
            if not best_draw:
                continue

            oX = best_draw.get("draw_odds")
            if not oX:
                continue

            used_bookies = set([
                str(best_home.get("bookmaker_id")), 
                str(best_draw.get("bookmaker_id")), 
                str(best_away.get("bookmaker_id"))
            ])
            if len(used_bookies) < 2:
                continue

            arb_margin = (1.0 / o1) + (1.0 / oX) + (1.0 / o2)

            if 0.85 < arb_margin < 1.0:
                profit_pct = round((1.0 - arb_margin) * 100, 2)

                stake_1 = round(bankroll / (o1 * arb_margin), -1)
                stake_X = round(bankroll / (oX * arb_margin), -1)
                stake_2 = round(bankroll / (o2 * arb_margin), -1)
                total_invested = stake_1 + stake_X + stake_2

                payout_1 = round(stake_1 * o1, 2)
                payout_X = round(stake_X * oX, 2)
                payout_2 = round(stake_2 * o2, 2)
                min_payout = min(payout_1, payout_X, payout_2)
                net_profit = round(min_payout - total_invested, 2)

                opportunities.append({
                    "sport": sport.upper(),
                    "competition": competition,
                    "event": f"{home_name} vs {away_name}",
                    "market_type": "3-Way 1X2",
                    "profit_margin_pct": profit_pct,
                    "total_investment": total_invested,
                    "guaranteed_net_profit": net_profit,
                    "guaranteed_payout": min_payout,
                    "legs": [
                        {
                            "outcome": f"1 ({home_name})",
                            "bookmaker": str(best_home.get("bookmaker_id", "")).upper(),
                            "odds": o1,
                            "recommended_stake": stake_1,
                            "expected_payout": payout_1
                        },
                        {
                            "outcome": "X (Draw)",
                            "bookmaker": str(best_draw.get("bookmaker_id", "")).upper(),
                            "odds": oX,
                            "recommended_stake": stake_X,
                            "expected_payout": payout_X
                        },
                        {
                            "outcome": f"2 ({away_name})",
                            "bookmaker": str(best_away.get("bookmaker_id", "")).upper(),
                            "odds": o2,
                            "recommended_stake": stake_2,
                            "expected_payout": payout_2
                        }
                    ]
                })

    opportunities.sort(key=lambda x: x["profit_margin_pct"], reverse=True)
    return opportunities


# -------------------------------------------------------------------
# BOOKMAKER REGISTRY (CONVERTED TO DIRECT REST ENDPOINTS)
# -------------------------------------------------------------------

BOOKMAKER_REGISTRY = {
    # Tier 1: Public REST APIs
    "betika": {"platform": "public_rest", "url": "https://api.betika.com/v1/uo/matches?limit=100&sub_type=prematch", "parser": "betika"},
    "sportybet": {"platform": "public_rest", "url": "https://www.sportybet.com/api/tz/factsCenter/pcUpcomingEvents?sportId=sr%3Asport%3A1&marketId=1%2C18%2C10%2C29%2C11%2C26%2C36%2C14%2C60100&pageSize=100&pageNum=1&option=1", "parser": "sportybet"},
    "bangbet": {"platform": "public_rest", "url": "https://bet-api.bangbet.com/api/bet/match/listTop?country=tz", "parser": "bangbet"},
    "sportpesa": {"platform": "public_rest", "url": "https://www.sportpesa.co.tz/api/upcoming/games?sportId=1", "parser": "sportpesa"},
    "leonbet": {"platform": "public_rest", "url": "https://leonbet.co.tz/api-2/betline/events/all?ctag=en-US", "parser": "leonbet"},
    "premierbet": {"platform": "public_rest", "url": "https://sports-api.premierbet.co.tz/v1/events/highlights?country=TZ&group=g2&platform=desktop&locale=en&sportId=1&limit=50", "parser": "premierbet"},

    # Converted Direct APIs for 1xBet Platform Clones (Get1x2_CompressZip)
    "1xbet": {"platform": "public_rest", "url": "https://1xbet.co.tz/LineFeed/Get1x2_CompressZip?sports=1&count=50&lng=en&mode=4", "parser": "1xcorp"},
    "22bet": {"platform": "public_rest", "url": "https://22bet.co.tz/LineFeed/Get1x2_CompressZip?sports=1&count=50&lng=en&mode=4", "parser": "1xcorp"},
    "betwinner": {"platform": "public_rest", "url": "https://betwinner.co.tz/LineFeed/Get1x2_CompressZip?sports=1&count=50&lng=en&mode=4", "parser": "1xcorp"},
    "helabet": {"platform": "public_rest", "url": "https://helabet.co.tz/LineFeed/Get1x2_CompressZip?sports=1&count=50&lng=en&mode=4", "parser": "1xcorp"},
    "1xbit": {"platform": "public_rest", "url": "https://1xbit.com/LineFeed/Get1x2_CompressZip?sports=1&count=50&lng=en&mode=4", "parser": "1xcorp"},
    "melbet": {"platform": "public_rest", "url": "https://melbet.co.tz/LineFeed/Get1x2_CompressZip?sports=1&count=50&lng=en&mode=4", "parser": "1xcorp"},

    # Converted Direct API for MeridianBet
    "meridianbet": {"platform": "public_rest", "url": "https://meridianbet.co.tz/api/v1/events/highlights", "parser": "meridianbet"},

    # Tier 2: Remaining SPA Targets via Playwright
    "galsport": {"platform": "playwright_spa", "url": "https://gsb.co.tz/en/sportsbook/highlights", "keywords": ["/api/", "highlights", "events", "sportsbook", "get", "fixtures"], "parser": "generic"},
    "parimatch": {"platform": "playwright_spa", "url": "https://parimatch.co.tz/en/football", "keywords": ["prematch", "events", "sportsbook", "/api/", "line"], "parser": "generic"},
    "betway": {"platform": "playwright_spa", "url": "https://www.betway.co.tz/sport/soccer", "keywords": ["highlights", "sportsapi", "event", "betbook"], "parser": "generic"},
}

BOOKMAKER_MAP = {bm: None for bm in BOOKMAKER_REGISTRY.keys()}


# -------------------------------------------------------------------
# PARSER ENGINE
# -------------------------------------------------------------------

def auto_detect_parser(payload: Any) -> str:
    if isinstance(payload, dict):
        if "Value" in payload or "LE" in payload: return "1xcorp"
        if "home_team" in payload or "home_odd" in payload: return "betika"
        data_obj = payload.get("data")
        if isinstance(data_obj, dict):
            if "tournaments" in data_obj: return "sportybet"
            if "groupList" in data_obj or "matchVoList" in data_obj: return "bangbet"
            if "categories" in data_obj: return "premierbet"
        if "events" in payload:
            sample_ev = payload.get("events", [{}])
            if isinstance(sample_ev, list) and len(sample_ev) > 0 and isinstance(sample_ev[0], dict):
                if "homeTeam" in sample_ev[0] or "markets" in sample_ev[0]:
                    return "meridianbet" if "homeTeam" in sample_ev[0] else "leonbet"
            return "leonbet"
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
        # 1. 1XCORP CLONES
        if parser_type == "1xcorp":
            val = payload.get("Value", []) if isinstance(payload, dict) else []
            events = val.get("Events") or val.get("G") or val.get("Games") or val.get("E") or [] if isinstance(val, dict) else (val if isinstance(val, list) else [])

            for item in events:
                if isinstance(item, dict):
                    home = extract_team_name(item.get("O1") or item.get("HT") or item.get("HomeTeam"))
                    away = extract_team_name(item.get("O2") or item.get("AT") or item.get("AwayTeam"))
                    
                    raw_sport_id = item.get("SI") or item.get("SportId") or item.get("SN")
                    detected_sport = resolve_sport_name(raw_sport_id)
                    competition = str(item.get("LE") or item.get("League") or "Unknown")

                    o1, oX, o2 = None, None, None
                    for outcome in item.get("E", []):
                        if isinstance(outcome, dict):
                            t = outcome.get("T")
                            if t == 1: o1 = safe_float(outcome.get("C"))
                            elif t == 2: oX = safe_float(outcome.get("C"))
                            elif t == 3: o2 = safe_float(outcome.get("C"))

                    raw_parsed.append({
                        "match_id": str(item.get("I") or item.get("ID") or ""),
                        "home_team": home, "away_team": away,
                        "competition": competition,
                        "home_odds": o1, "draw_odds": oX, "away_odds": o2,
                        "sport": detected_sport, "market_type": "1X2" if oX is not None else "2WAY",
                        "bookmaker_id": bookmaker_id, "timestamp": ts, "latency_ms": latency_ms
                    })

        # 2. MERIDIANBET DIRECT
        elif parser_type == "meridianbet":
            events = payload.get("events", []) if isinstance(payload, dict) else (payload if isinstance(payload, list) else [])
            for item in events:
                if isinstance(item, dict):
                    home = extract_team_name(item.get("homeTeam") or item.get("home_team"))
                    away = extract_team_name(item.get("awayTeam") or item.get("away_team"))
                    comp = str(item.get("league", {}).get("name") if isinstance(item.get("league"), dict) else (item.get("competition") or "Soccer"))

                    o1, oX, o2 = None, None, None
                    markets = item.get("markets", [])
                    if isinstance(markets, list) and len(markets) > 0:
                        selections = markets[0].get("selections", []) or markets[0].get("outcomes", [])
                        if isinstance(selections, list):
                            for sel in selections:
                                if isinstance(sel, dict):
                                    sel_name = str(sel.get("name") or sel.get("type", "")).upper()
                                    price = safe_float(sel.get("price") or sel.get("odds"))
                                    if sel_name in ["1", "HOME"]: o1 = price
                                    elif sel_name in ["X", "DRAW"]: oX = price
                                    elif sel_name in ["2", "AWAY"]: o2 = price

                    raw_parsed.append({
                        "match_id": str(item.get("id") or ""),
                        "home_team": home, "away_team": away,
                        "competition": comp,
                        "home_odds": o1, "draw_odds": oX, "away_odds": o2,
                        "sport": "soccer", "market_type": "1X2",
                        "bookmaker_id": bookmaker_id, "timestamp": ts, "latency_ms": latency_ms
                    })

        # 3. BETIKA
        elif parser_type == "betika":
            events = payload.get("data", []) if isinstance(payload, dict) else (payload if isinstance(payload, list) else [])
            for item in events:
                if isinstance(item, dict):
                    raw_parsed.append({
                        "match_id": str(item.get("match_id") or item.get("game_id") or ""),
                        "home_team": extract_team_name(item.get("home_team")),
                        "away_team": extract_team_name(item.get("away_team")),
                        "competition": str(item.get("competition_name") or "Unknown"),
                        "home_odds": safe_float(item.get("home_odd")),
                        "draw_odds": safe_float(item.get("neutral_odd")),
                        "away_odds": safe_float(item.get("away_odd")),
                        "sport": "soccer", "market_type": "1X2",
                        "bookmaker_id": bookmaker_id, "timestamp": ts, "latency_ms": latency_ms
                    })

        # 4. LEONBET
        elif parser_type == "leonbet":
            events = payload.get("events", []) if isinstance(payload, dict) else []
            for item in events:
                if isinstance(item, dict):
                    home, away = "", ""
                    competitors = item.get("competitors", [])
                    if isinstance(competitors, list) and len(competitors) >= 2:
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
                    markets = item.get("markets", [])
                    if isinstance(markets, list):
                        for market in markets:
                            if isinstance(market, dict):
                                m_name = str(market.get("name", "")).upper()
                                m_type = str(market.get("type", "")).upper()
                                
                                if "1X2" in m_name or "WINNER" in m_name or "1X2" in m_type or market.get("primary") is True:
                                    runners = market.get("runners", [])
                                    if isinstance(runners, list):
                                        for runner in runners:
                                            if isinstance(runner, dict):
                                                price = safe_float(runner.get("price") or runner.get("priceStr") or runner.get("odd"))
                                                r_type = str(runner.get("type") or runner.get("name") or "").upper()
                                                tags = [str(t).upper() for t in runner.get("tags", []) if t]

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
                            "competition": str(item.get("league", {}).get("name") if isinstance(item.get("league"), dict) else (item.get("family", {}).get("name") if isinstance(item.get("family"), dict) else "Unknown")),
                            "home_odds": o1, "draw_odds": oX, "away_odds": o2,
                            "sport": "soccer", "market_type": "1X2",
                            "bookmaker_id": bookmaker_id, "timestamp": ts, "latency_ms": latency_ms
                        })

        # 5. PREMIERBET
        elif parser_type == "premierbet":
            categories = payload.get("data", {}).get("categories", []) if isinstance(payload, dict) and isinstance(payload.get("data"), dict) else []
            for cat in categories:
                if isinstance(cat, dict):
                    for comp in cat.get("competitions", []):
                        if isinstance(comp, dict):
                            comp_name = comp.get("name") or "Unknown"
                            for event in comp.get("events", []):
                                if isinstance(event, dict):
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
                                            if isinstance(mg, dict):
                                                markets.extend(mg.get("markets", []))

                                    for market in markets:
                                        if isinstance(market, dict):
                                            selections = market.get("selections") or market.get("outcomes") or market.get("betOffers") or []
                                            if isinstance(selections, list):
                                                for idx, sel in enumerate(selections):
                                                    if isinstance(sel, dict):
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

        # 6. BANGBET
        elif parser_type == "bangbet":
            groups = payload.get("data", {}).get("groupList", []) if isinstance(payload, dict) and isinstance(payload.get("data"), dict) else []
            for group in groups:
                if isinstance(group, dict):
                    match_list = group.get("matchVoList") or group.get("matchList") or []
                    for match in match_list:
                        if isinstance(match, dict):
                            home = extract_team_name(match.get("homeTeamName") or match.get("homeName") or match.get("homeTeam"))
                            away = extract_team_name(match.get("awayTeamName") or match.get("awayName") or match.get("awayTeam"))

                            o1, oX, o2 = None, None, None
                            market_list = match.get("marketList", [])
                            if isinstance(market_list, list):
                                for market_group in market_list:
                                    if isinstance(market_group, dict):
                                        inner_markets = market_group.get("markets") or ([market_group] if market_group.get("outcomes") else [])
                                        for market in inner_markets:
                                            if isinstance(market, dict):
                                                m_name = str(market.get("name") or market.get("marketName") or "").upper()
                                                if not ("1X2" in m_name or "3-WAY" in m_name or str(market.get("id")) == "1"): continue

                                                outcomes = market.get("outcomes") or market.get("optionList") or market.get("options") or []
                                                if isinstance(outcomes, list):
                                                    for idx, outcome in enumerate(outcomes):
                                                        if isinstance(outcome, dict):
                                                            desc_upper = str(outcome.get("desc") or outcome.get("type") or outcome.get("name") or "").strip().upper()
                                                            raw_price = outcome.get("odds") or outcome.get("price") or outcome.get("val")

                                                            try: raw_val = float(raw_price) if raw_price is not None else None
                                                            except (ValueError, TypeError): raw_val = None

                                                            price = safe_float(raw_val / 1000.0) if (raw_val and raw_val >= 100) else safe_float(raw_val)

                                                            if desc_upper in ["DRAW", "X"]:
                                                                if oX is None: oX = price
                                                            elif desc_upper in ["1", "HOME"] or (home and home.upper() == desc_upper) or idx == 0:
                                                                if o1 is None: o1 = price
                                                            elif desc_upper in ["2", "AWAY"] or (away and away.upper() == desc_upper) or idx == 2:
                                                                if o2 is None: o2 = price

                            raw_parsed.append({
                                "match_id": str(match.get("id") or match.get("matchId") or ""),
                                "home_team": home, "away_team": away,
                                "competition": str(match.get("tournamentName") or match.get("leagueName") or "Unknown"),
                                "home_odds": o1, "draw_odds": oX, "away_odds": o2,
                                "sport": "soccer", "market_type": "1X2",
                                "bookmaker_id": bookmaker_id, "timestamp": ts, "latency_ms": latency_ms
                            })

        # 7. SPORTYBET
        elif parser_type == "sportybet":
            data_obj = payload.get("data", {}) if isinstance(payload, dict) else {}
            tournaments = data_obj.get("tournaments", []) or data_obj.get("events", []) if isinstance(data_obj, dict) else []
            for tourney in tournaments:
                if isinstance(tourney, dict):
                    events = tourney.get("events", []) if isinstance(tourney, dict) else [tourney]
                    for item in events:
                        if isinstance(item, dict):
                            home = extract_team_name(item.get("homeTeamName") or item.get("homeTeam"))
                            away = extract_team_name(item.get("awayTeamName") or item.get("awayTeam"))
                            o1, oX, o2 = None, None, None
                            markets = item.get("markets", [])
                            if isinstance(markets, list):
                                for market in markets:
                                    if isinstance(market, dict) and (str(market.get("id")) in ["1", "10"] or market.get("name") in ["1X2", "3-Way"]):
                                        outcomes = market.get("outcomes", [])
                                        if isinstance(outcomes, list):
                                            for outcome in outcomes:
                                                if isinstance(outcome, dict):
                                                    desc = str(outcome.get("desc") or outcome.get("outcomeName"))
                                                    price = safe_float(outcome.get("odds"))
                                                    if desc in ["1", "Home"]: o1 = price
                                                    elif desc in ["X", "Draw"]: oX = price
                                                    elif desc in ["2", "Away"]: o2 = price

                            raw_parsed.append({
                                "match_id": str(item.get("eventId") or item.get("id") or ""),
                                "home_team": home, "away_team": away,
                                "competition": str(tourney.get("name") or "Unknown"),
                                "home_odds": o1, "draw_odds": oX, "away_odds": o2,
                                "sport": "soccer", "market_type": "1X2",
                                "bookmaker_id": bookmaker_id, "timestamp": ts, "latency_ms": latency_ms
                            })

        # 8. SPORTPESA
        elif parser_type == "sportpesa":
            games = payload if isinstance(payload, list) else (payload.get("data") or payload.get("games") or payload.get("events") or []) if isinstance(payload, dict) else []
            for item in games:
                if isinstance(item, dict):
                    home = extract_team_name(item.get("homeTeam") or item.get("home_team"))
                    away = extract_team_name(item.get("awayTeam") or item.get("away_team"))
                    o1, oX, o2 = None, None, None
                    markets = item.get("markets") or item.get("marketsList") or []
                    if isinstance(markets, list):
                        for market in markets:
                            if isinstance(market, dict):
                                m_id = str(market.get("id") or "")
                                m_name = str(market.get("name", "")).upper()
                                if m_id in ["10", "1"] or "1X2" in m_name or "3-WAY" in m_name:
                                    selections = market.get("selections", [])
                                    if isinstance(selections, list):
                                        for sel in selections:
                                            if isinstance(sel, dict):
                                                sel_type = str(sel.get("type") or sel.get("name", "")).upper()
                                                price = safe_float(sel.get("odds") or sel.get("price"))
                                                if sel_type in ["1", "HOME"]: o1 = price
                                                elif sel_type in ["X", "DRAW"]: oX = price
                                                elif sel_type in ["2", "AWAY"]: o2 = price

                    raw_parsed.append({
                        "match_id": str(item.get("gameId") or item.get("id") or ""),
                        "home_team": home, "away_team": away,
                        "competition": str(item.get("competition", {}).get("name") if isinstance(item.get("competition"), dict) else (item.get("competition") or "Unknown")),
                        "home_odds": o1, "draw_odds": oX, "away_odds": o2,
                        "sport": "soccer", "market_type": "1X2",
                        "bookmaker_id": bookmaker_id, "timestamp": ts, "latency_ms": latency_ms
                    })

        # 9. RECURSIVE GENERIC FALLBACK
        else:
            events = find_events_recursive(payload)
            for item in events:
                if isinstance(item, dict):
                    home = extract_team_name(item.get("homeTeam") or item.get("home_team") or item.get("homeName") or item.get("team1"))
                    away = extract_team_name(item.get("awayTeam") or item.get("away_team") or item.get("awayName") or item.get("team2"))
                    
                    raw_sport = item.get("sportId") or item.get("sport") or item.get("sportName") or item.get("categoryName")
                    detected_sport = resolve_sport_name(raw_sport)
                    competition = str(item.get("league") or item.get("competition") or item.get("leagueName") or "Unknown")

                    raw_odds = item.get("odds") if isinstance(item.get("odds"), dict) else {}
                    o1 = safe_float(item.get("home_odds") or item.get("homeOdds") or item.get("odds1") or raw_odds.get("1"))
                    oX = safe_float(item.get("draw_odds") or item.get("drawOdds") or item.get("oddsX") or raw_odds.get("X"))
                    o2 = safe_float(item.get("away_odds") or item.get("awayOdds") or item.get("odds2") or raw_odds.get("2"))

                    raw_parsed.append({
                        "match_id": str(item.get("id") or item.get("eventId") or item.get("match_id") or ""),
                        "home_team": home, "away_team": away,
                        "competition": competition,
                        "home_odds": o1, "draw_odds": oX, "away_odds": o2,
                        "sport": detected_sport, "market_type": "1X2" if oX is not None else "2WAY",
                        "bookmaker_id": bookmaker_id, "timestamp": ts, "latency_ms": latency_ms
                    })

        matches = [m for m in raw_parsed if isinstance(m, dict) and validate_match(m)]

        if len(matches) > 0:
            counts = Counter(m["sport"] for m in matches)
            breakdown = ", ".join(f"{sp}: {cnt}" for sp, cnt in counts.items())
            logger.info(f"[{bookmaker_id.upper()}] Parsed {len(matches)} valid matches ({breakdown})")

    except Exception as e:
        logger.error(f"[{bookmaker_id}] Parser Exception ({type(e).__name__}): {repr(e)}")

    return matches


# -------------------------------------------------------------------
# HARDENED HTTP FETCHER WITH FAST 404 EXIT
# -------------------------------------------------------------------

async def fetch_http_api(session: AsyncSession, bookmaker_id: str, config: dict, retries: int = 3) -> List[Dict[str, Any]]:
    url = config["url"]
    headers = get_dynamic_headers(url)

    async with HTTP_SEMAPHORE:
        for attempt in range(retries):
            try:
                res = await session.get(url, headers=headers, impersonate="chrome", timeout=10)
                
                # Fast Exit on 404 Not Found
                if res.status_code == 404:
                    logger.warning(f"[{bookmaker_id.upper()}] 404 Not Found on {url}. Aborting retries immediately.")
                    return []

                if bookmaker_id == "sportpesa":
                    if res.status_code in [200, 203]:
                        try:
                            data_init = res.json()
                            games_list = data_init if isinstance(data_init, list) else (data_init.get("data") or data_init.get("games") or data_init.get("items") or [])
                            game_ids = [str(g.get("id") or g.get("gameId")) for g in games_list if isinstance(g, dict) and (g.get("id") or g.get("gameId"))][:30]
                            
                            if game_ids:
                                markets_url = f"https://www.sportpesa.co.tz/api/games/markets?games={','.join(game_ids)}&markets=10"
                                res_markets = await session.get(markets_url, headers=headers, impersonate="chrome", timeout=10)
                                if res_markets.status_code in [200, 203]:
                                    return parse_raw_payload(bookmaker_id, res_markets.json())
                                else:
                                    logger.warning(f"[SPORTPESA] Markets status {res_markets.status_code} (Attempt {attempt+1}/{retries})")
                            else:
                                logger.warning(f"[SPORTPESA] No game IDs found (Attempt {attempt+1}/{retries})")
                        except Exception as parse_err:
                            logger.warning(f"[SPORTPESA] JSON parse error: {parse_err} (Attempt {attempt+1}/{retries})")
                    else:
                        logger.warning(f"[SPORTPESA] Highlights status {res.status_code} (Attempt {attempt+1}/{retries})")
                else:
                    if res.status_code in [200, 203]:
                        try:
                            return parse_raw_payload(bookmaker_id, res.json())
                        except Exception:
                            preview = res.text[:200].replace("\n", " ")
                            logger.warning(f"[{bookmaker_id.upper()}] Non-JSON body on HTTP {res.status_code}: {preview} (Attempt {attempt+1}/{retries})")
                    else:
                        logger.warning(f"[{bookmaker_id.upper()}] HTTP Status {res.status_code} (Attempt {attempt+1}/{retries})")
            except Exception as e:
                logger.error(f"[{bookmaker_id.upper()}] Fetch Exception: {repr(e)} (Attempt {attempt+1}/{retries})")
            
            await asyncio.sleep(1.0 * (attempt + 1))
    return []


# -------------------------------------------------------------------
# PLAYWRIGHT INTERCEPTOR WITH SAFE DIAGNOSTIC LOGGING
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
                user_agent=DESKTOP_USER_AGENT,
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

            page = await context.new_page()

            async def block_unnecessary_resources(route):
                if route.request.resource_type in ["image", "font", "media"]:
                    await route.abort()
                else:
                    await route.continue_()

            await page.route("**/*", block_unnecessary_resources)

            async def handle_response(response):
                if response.status in [200, 203]:
                    res_url = response.url.lower()

                    if "growthbook" not in res_url and "features" not in res_url and "analytics" not in res_url:
                        if "config" not in res_url and "contacts" not in res_url:
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
                                    if isinstance(json_data, dict):
                                        keys_sample = list(json_data.keys())[:8]
                                        logger.debug(f"[{bm_label}-CAPTURED] {response.url} | Dict Keys: {keys_sample}")
                                    elif isinstance(json_data, list):
                                        logger.debug(f"[{bm_label}-CAPTURED] {response.url} | List Count: {len(json_data)}")

            page.on("response", handle_response)

            def handle_websocket(ws):
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
                        except Exception:
                            pass

                ws.on("framereceived", handle_frame)

            page.on("websocket", handle_websocket)
            logger.info(f"[{bm_label}-INTERCEPTOR] Navigating to {url}...")

            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=12000)

                try:
                    await page.wait_for_load_state("networkidle", timeout=6000)
                except Exception:
                    pass

                await page.mouse.move(300, 400)
                await page.evaluate("window.scrollBy(0, 500)")
                await asyncio.sleep(3.0)
            except Exception:
                logger.warning(f"[{bm_label}-INTERCEPTOR] Navigation timeout warning, processing captured payloads...")

            await page.close()
            await context.close()

            latency_ms = int((time.time() - start_t) * 1000)

            logger.info(f"[{bm_label}-INTERCEPTOR] Captured {len(captured_payloads)} total network payloads.")

            for res_url, payload in captured_payloads:
                parsed = parse_raw_payload(bookmaker_id, payload, latency_ms=latency_ms)
                all_matches.extend(parsed)

            unique_matches = list({f"{m['bookmaker_id']}_{m['match_id']}": m for m in all_matches if isinstance(m, dict) and m.get("match_id")}.values()) if all_matches else []
            logger.info(f"[{bm_label}-INTERCEPTOR] Parsed {len(unique_matches)} unique valid matches in {latency_ms}ms.")
            return unique_matches

        except Exception as e:
            logger.error(f"[{bookmaker_id}-INTERCEPTOR] Error ({type(e).__name__}): {repr(e)}")
    return []


# -------------------------------------------------------------------
# DISPATCHER MASTER SCANNER LOOP
# -------------------------------------------------------------------

async def scrape_all_sportsbooks() -> Dict[str, Any]:
    all_matches = []

    # 1. Fast Parallel HTTP Scrapers
    http_targets = {bm: cfg for bm, cfg in BOOKMAKER_REGISTRY.items() if cfg["platform"] in ["public_rest"]}

    async with AsyncSession() as session:
        http_tasks = [fetch_http_api(session, bm, cfg) for bm, cfg in http_targets.items()]
        http_results = await asyncio.gather(*http_tasks, return_exceptions=True)
        for res in http_results:
            if isinstance(res, list):
                all_matches.extend([x for x in res if isinstance(x, dict)])

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
                    all_matches.extend([x for x in res if isinstance(x, dict)])
            await browser.close()
    except Exception as e:
        logger.error(f"Playwright Execution Batch Error: {repr(e)}")

    # 3. Calculate True Cross-Bookmaker Arbitrage
    arbitrage_ops = find_arbitrage_opportunities(all_matches, bankroll=100000.0)

    logger.info(f"=== SCAN COMPLETED: Extracted {len(all_matches)} matches | Found {len(arbitrage_ops)} True Cross-Bookmaker Surebets ===")

    for idx, arb in enumerate(arbitrage_ops[:5], 1):
        logger.info(f"\n⚡ [SUREBET #{idx}] +{arb['profit_margin_pct']}% Profit Margin")
        logger.info(f"   Event: {arb['event']} [{arb['competition']}] ({arb['sport']})")
        logger.info(f"   Bankroll: {arb['total_investment']:,.0f} TZS | Payout: {arb['guaranteed_payout']:,.0f} TZS (+{arb['guaranteed_net_profit']:,.0f} TZS Net Profit)")
        for leg in arb['legs']:
            logger.info(f"   👉 Bet {leg['recommended_stake']:,.0f} TZS on [{leg['bookmaker']}] @ {leg['odds']} for {leg['outcome']}")

    return {
        "matches_count": len(all_matches),
        "arbitrage_opportunities_count": len(arbitrage_ops),
        "arbitrage_opportunities": arbitrage_ops,
        "matches": all_matches
    }
