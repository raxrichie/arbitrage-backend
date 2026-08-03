import asyncio
import json
import logging
import os
import re
import time
import zlib
from collections import Counter
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

from curl_cffi.requests import AsyncSession
from tenacity import (
    retry,
    stop_after_attempt,
    wait_exponential,
    retry_if_exception,
    before_sleep_log,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("scrapers")

# Feature Flags & Cache Configuration
NETWORK_RECORDER = os.getenv("NETWORK_RECORDER", "false").lower() == "true"
SAVE_DIAGNOSTICS = os.getenv("SAVE_DIAGNOSTICS", "true").lower() == "true"
DIAGNOSTICS_DIR = "diagnostics"
CACHE_FILE = "endpoints_cache.json"
CACHE_TTL_SECONDS = 3600 * 24  # 24 hours

if SAVE_DIAGNOSTICS and not os.path.exists(DIAGNOSTICS_DIR):
    os.makedirs(DIAGNOSTICS_DIR, exist_ok=True)

# Ignore infrastructure endpoints before payload scoring
IGNORE_JSON_KEYWORDS = (
    "growthbook", "analytics", "geo-location", "config", "translations",
    "feature", "consent", "cookies", "telemetry", "google-analytics", "facebook"
)

# Stealth browser patchright initialization
try:
    from patchright.async_api import Browser, async_playwright
    logger.info("Using patchright for browser automation (stealth fork enabled).")
except ImportError:
    from playwright.async_api import Browser, async_playwright
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


# -------------------------------------------------------------------
# ENDPOINT DISCOVERY CACHE ENGINE
# -------------------------------------------------------------------

def load_endpoint_cache() -> Dict[str, Any]:
    if os.path.exists(CACHE_FILE):
        try:
            with open(CACHE_FILE, "r", encoding="utf-8") as f:
                cache_data = json.load(f)
                current_time = time.time()
                cleaned_cache = {}
                for bm_id, entry in cache_data.items():
                    if "last_verified" in entry:
                        last_verified_ts = time.mktime(time.strptime(entry["last_verified"], "%Y-%m-%dT%H:%M:%SZ"))
                        if current_time - last_verified_ts < CACHE_TTL_SECONDS:
                            cleaned_cache[bm_id] = entry
                return cleaned_cache
        except Exception as e:
            logger.error(f"Failed to load or clean endpoint cache: {repr(e)}")
            pass
    return {}


def update_endpoint_cache(bookmaker_id: str, endpoint_url: str, score: int):
    cache = load_endpoint_cache()
    cache[bookmaker_id] = {
        "endpoint": endpoint_url,
        "last_verified": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "score": score
    }
    try:
        with open(CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(cache, f, indent=2)
        logger.info(f"[{bookmaker_id.upper()}-CACHE-UPDATED] Saved endpoint: {endpoint_url} with score {score}")
    except Exception as e:
        logger.error(f"[{bookmaker_id.upper()}-CACHE-ERROR] Failed to save endpoint cache: {repr(e)}")


# -------------------------------------------------------------------
# UTILITY FUNCTIONS & STRUCTURAL SCORING
# -------------------------------------------------------------------

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
    return val_str.replace(" ", "_")


def generate_event_fingerprint(home: str, away: str, sport: str) -> str:
    ignored_words = {"fc", "cf", "united", "city", "town", "real", "athletic", "club", "sc", "sporting", "st", "saint", "afc", "ec"}
    
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

    virtual_keywords = ["zoom", "virtual", "cyber", "simulated", "srl", "esoccer", "eleague"]
    if any(k in comp or k in home.lower() or k in away.lower() for k in virtual_keywords):
        return False

    if len(home) < 2 or len(away) < 2 or home.startswith("{") or away.startswith("{") or home == away:
        return False

    o1 = match.get("home_odds")
    oX = match.get("draw_odds")
    o2 = match.get("away_odds")

    valid_odds = [o for o in [o1, oX, o2] if o is not None and isinstance(o, (int, float)) and o > 1.01]
    sport = resolve_sport_name(match.get("sport", "soccer"))

    if sport in ["tennis", "basketball", "volleyball", "mma", "table_tennis", "baseball", "darts", "handball"]:
        return len(valid_odds) >= 2
    else:
        return len(valid_odds) >= 2 and oX is not None


def score_sportsbook_payload(obj: Any) -> int:
    score = 0
    obj_str = str(obj).lower()
    keywords = ["home_team", "away_team", "competitors", "eventnames", "odds", "markets", "outcomes", "1x2", "coef", "runners", "price", "value"]
    for kw in keywords:
        score += obj_str.count(kw)

    if isinstance(obj, (dict, list)):
        events = find_events_recursive(obj)
        score += len(events) * 5
    return score


def extract_ssr_hydration_json(html_content: str) -> List[Dict[str, Any]]:
    found_payloads = []
    ssr_patterns = [
        r'<script[^>]*id="__NEXT_DATA__"[^>]*>(.*?)</script>',
        r'<script[^>]*id="__NUXT__"[^>]*>(.*?)</script>',
        r'window\.__INITIAL_STATE__\s*=\s*({.*?});',
        r'window\.__PRELOADED_STATE__\s*=\s*({.*?});',
        r'window\.__APOLLO_STATE__\s*=\s*({.*?});'
    ]
    for pattern in ssr_patterns:
        matches = re.findall(pattern, html_content, re.DOTALL)
        for match in matches:
            try:
                data = json.loads(match.strip())
                if score_sportsbook_payload(data) > 10:
                    found_payloads.append(data)
            except Exception as e:
                logger.warning(f"Failed to parse SSR JSON payload: {repr(e)}")
    return found_payloads


def find_events_recursive(obj: Any, depth: int = 0) -> List[Dict[str, Any]]:
    if depth > 4:
        return []
    if isinstance(obj, list):
        if len(obj) > 0 and isinstance(obj[0], dict):
            sample = obj[0]
            if any(k in sample for k in ["home_team", "homeTeam", "competitors", "eventNames", "O1", "teams", "eName", "games", "matchId"]):
                return [x for x in obj if isinstance(x, dict)]
        events = []
        for item in obj[:10]:
            if isinstance(item, (dict, list)):
                found = find_events_recursive(item, depth + 1)
                if found:
                    events.extend(found)
        return events
    elif isinstance(obj, dict):
        for key in ["events", "matches", "games", "fixtures", "items", "groupList", "matchVoList", "data", "sportsTree", "results"]:
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
        home = str(m.get("home_team", "")).strip()
        away = str(m.get("away_team", "")).strip()
        sport = str(m.get("sport", "soccer"))
        if not home or not away or home == away:
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
        home_name = valid_matches[0].get("home_team", "").strip()
        away_name = valid_matches[0].get("away_team", "").strip()
        competition = valid_matches[0].get("competition", "Unknown")

        best_home = max([m for m in valid_matches if m.get("home_odds") is not None], key=lambda x: x.get("home_odds") or 0, default=None)
        best_away = max([m for m in valid_matches if m.get("away_odds") is not None], key=lambda x: x.get("away_odds") or 0, default=None)

        o1 = best_home.get("home_odds") if best_home else None
        o2 = best_away.get("away_odds") if best_away else None

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
# BOOKMAKER REGISTRY
# -------------------------------------------------------------------

BOOKMAKER_REGISTRY = {
    # Direct Public REST APIs
    "betika": {"platform": "public_rest", "url": "https://api.betika.com/v1/uo/matches?limit=100&sub_type=prematch", "parser": "betika", "timeout": 8},
    "sportybet": {"platform": "public_rest", "url": "https://www.sportybet.com/api/tz/factsCenter/pcUpcomingEvents?pageSize=100&pageNum=1&option=1", "parser": "sportybet", "timeout": 8},
    "bangbet": {"platform": "public_rest", "url": "https://bet-api.bangbet.com/api/bet/match/listTop?country=tz", "parser": "bangbet", "timeout": 8},
    "leonbet": {"platform": "public_rest", "url": "https://leonbet.co.tz/api-2/betline/events/all?ctag=en-US", "parser": "leonbet", "timeout": 25},
    "premierbet": {"platform": "public_rest", "url": "https://sports-api.premierbet.co.tz/v1/events/highlights?country=TZ&group=g2&platform=desktop&locale=sw&limit=100", "parser": "premierbet", "timeout": 8},

    # ALL 1XCORP CLONES (Direct REST)
    "22bet": {"platform": "public_rest", "url": "https://22bet.co.tz/service-api/LiveFeed/Get1x2_VZip?count=50&lng=en_GB&gr=329&mode=4&country=181&partner=151", "parser": "1xcorp", "timeout": 10},
    "helabet": {"platform": "public_rest", "url": "https://helabet.co.tz/service-api/LiveFeed/Get1x2_VZip?count=50&lng=en&gr=329&mode=4&country=181&partner=237", "parser": "1xcorp", "timeout": 10},
    "betwinner": {"platform": "public_rest", "url": "https://betwinner.co.tz/service-api/LiveFeed/Get1x2_VZip?count=50&lng=en&gr=329&mode=4&country=181&partner=777", "parser": "1xcorp", "timeout": 10},
    "1xbet": {"platform": "public_rest", "url": "https://1xbet.co.tz/service-api/LiveFeed/Get1x2_VZip?count=50&lng=en&gr=329&mode=4&country=181&partner=1499", "parser": "1xcorp", "timeout": 10},
    "1xbit": {"platform": "public_rest", "url": "https://1xbit.com/service-api/LiveFeed/Get1x2_VZip?count=50&lng=en&gr=329&mode=4&country=181&partner=933", "parser": "1xcorp", "timeout": 10},
    "megapari": {"platform": "public_rest", "url": "https://megapari.com/service-api/LiveFeed/Get1x2_VZip?count=50&lng=en&gr=329&mode=4&country=181&partner=824", "parser": "1xcorp", "timeout": 10},
    "melbet": {"platform": "public_rest", "url": "https://melbet.co.tz/service-api/LiveFeed/Get1x2_VZip?count=50&lng=en&gr=329&mode=4&country=181&partner=151", "parser": "1xcorp", "timeout": 10},

    # Non-1XCorp SPA Targets via Stealth Playwright Interceptors
    "sportpesa": {"platform": "playwright_spa", "url": "https://www.sportpesa.co.tz/en/sports-betting/football-1/", "keywords": ["/api/", "games", "upcoming", "highlights"], "parser": "sportybet"},
    "meridianbet": {"platform": "playwright_spa", "url": "https://meridianbet.co.tz/en/betting/football", "keywords": ["/api/", "/events/", "betsapi", "standard", "v2"], "parser": "meridianbet"},
    "mbet": {"platform": "playwright_spa", "url": "https://mbet.co.tz", "keywords": ["/api/", "sportsbook", "matches", "events"], "parser": "generic"},
    "1win": {"platform": "playwright_spa", "url": "https://1win.co.tz", "keywords": ["/api/", "sports", "football", "matches"], "parser": "generic"},
    "kingbet": {"platform": "playwright_spa", "url": "https://www.kingbet.co.tz/en/sportsbook/highlights", "keywords": ["redis_data", "home", "events", "sportsbook"], "parser": "generic"},
    "galsport": {"platform": "playwright_spa", "url": "https://gsb.co.tz/en/sportsbook/highlights", "keywords": ["/api/", "highlights", "events", "sportsbook", "get", "fixtures", "evapi"], "parser": "generic"},
    "parimatch": {"platform": "playwright_spa", "url": "https://parimatch.co.tz/en/football/prematch", "keywords": ["prematch", "sportsbook", "events", "line"], "parser": "generic"},
    "betway": {"platform": "playwright_spa", "url": "https://www.betway.co.tz", "keywords": ["highlights", "sportsapi", "event", "betbook"], "parser": "generic"},
    "sokabet": {"platform": "playwright_spa", "url": "https://sokabet.co.tz", "keywords": ["api", "events", "highlights", "GetTopEvents", "altenar"], "parser": "generic"},
}

BOOKMAKER_MAP = {bm: None for bm in BOOKMAKER_REGISTRY.keys()}


# -------------------------------------------------------------------
# PARSER ENGINE
# -------------------------------------------------------------------

def parse_raw_payload(bookmaker_id: str, payload: Any, latency_ms: int = 0) -> List[Dict[str, Any]]:
    ts = int(time.time())
    config = BOOKMAKER_REGISTRY.get(bookmaker_id, {})
    parser_type = config.get("parser", "generic")
    raw_parsed = []

    try:
        if not isinstance(payload, (dict, list)):
            return []

        if isinstance(payload, dict) and any(str(k).startswith("-1") for k in payload.keys()):
            return []

        # 1. 1XCORP CLONES
        if parser_type == "1xcorp":
            events = []
            if isinstance(payload, list):
                events = payload
            elif isinstance(payload, dict):
                val = payload.get("Value") or payload.get("data") or payload
                if isinstance(val, dict):
                    events = val.get("Events") or val.get("G") or val.get("Games") or val.get("E") or []
                elif isinstance(val, list):
                    events = val

            for item in events:
                if isinstance(item, dict):
                    home = extract_team_name(item.get("O1") or item.get("HT") or item.get("HomeTeam") or item.get("O1Name"))
                    away = extract_team_name(item.get("O2") or item.get("AT") or item.get("AwayTeam") or item.get("O2Name"))

                    if not home or not away:
                        raw_name = str(item.get("N") or item.get("Name") or "")
                        if " - " in raw_name:
                            parts = raw_name.split(" - ", 1)
                            home, away = parts[0].strip(), parts[1].strip()
                        elif " vs " in raw_name.lower():
                            parts = raw_name.lower().split(" vs ", 1)
                            home, away = parts[0].strip(), parts[1].strip()

                    raw_sport_id = item.get("SI") or item.get("SportId") or item.get("SN")
                    detected_sport = resolve_sport_name(raw_sport_id)
                    competition = str(item.get("LE") or item.get("League") or item.get("L") or "Unknown")

                    o1, oX, o2 = None, None, None
                    raw_e = item.get("E") or item.get("Events") or item.get("Markets") or []

                    flat_outcomes = []
                    if isinstance(raw_e, list):
                        for element in raw_e:
                            if isinstance(element, list):
                                flat_outcomes.extend(element)
                            elif isinstance(element, dict):
                                flat_outcomes.append(element)

                    for outcome in flat_outcomes:
                        if isinstance(outcome, dict):
                            t = outcome.get("T") or outcome.get("Type")
                            price = safe_float(outcome.get("C") or outcome.get("Coef") or outcome.get("Price"))
                            if t in [1, "1"]: o1 = price
                            elif t in [2, "2", "X"]: oX = price
                            elif t in [3, "3", "2"]: o2 = price

                    if home and away:
                        raw_parsed.append({
                            "match_id": str(item.get("I") or item.get("ID") or item.get("Ci") or ""),
                            "home_team": home, "away_team": away,
                            "competition": competition,
                            "home_odds": o1, "draw_odds": oX, "away_odds": o2,
                            "sport": detected_sport, "market_type": "1X2" if oX is not None else "2WAY",
                            "bookmaker_id": bookmaker_id, "timestamp": ts, "latency_ms": latency_ms
                        })

        # 2. MERIDIANBET PARSER
        elif parser_type == "meridianbet":
            events_list = []
            if isinstance(payload, dict):
                sports = payload.get("sports", []) or [payload]
                for sport_obj in sports:
                    if isinstance(sport_obj, dict):
                        cats = sport_obj.get("categories", []) or [sport_obj]
                        for cat in cats:
                            if isinstance(cat, dict):
                                tourneys = cat.get("tournaments", []) or [cat]
                                for tourney in tourneys:
                                    if isinstance(tourney, dict):
                                        evs = tourney.get("events", [])
                                        if isinstance(evs, list):
                                            events_list.extend(evs)
            elif isinstance(payload, list):
                events_list = payload

            for item in events_list:
                if isinstance(item, dict):
                    home = extract_team_name(item.get("home") or item.get("homeTeam") or item.get("team1"))
                    away = extract_team_name(item.get("away") or item.get("awayTeam") or item.get("team2"))

                    if not home or not away:
                        name = str(item.get("name") or "")
                        if " - " in name:
                            parts = name.split(" - ", 1)
                            home, away = parts[0].strip(), parts[1].strip()

                    o1, oX, o2 = None, None, None
                    games = item.get("games", []) or item.get("markets", [])
                    if isinstance(games, list):
                        for game in games:
                            if isinstance(game, dict):
                                game_name = str(game.get("name") or game.get("code") or "").upper()
                                if "1X2" in game_name or "FINAL RESULT" in game_name or "WINNER" in game_name or game.get("isPrimary"):
                                    selections = game.get("selections") or game.get("outcomes") or []
                                    if isinstance(selections, list):
                                        for sel in selections:
                                            if isinstance(sel, dict):
                                                type_str = str(sel.get("type") or sel.get("name") or "").upper()
                                                price = safe_float(sel.get("price") or sel.get("odd") or sel.get("value"))
                                                if type_str in ["1", "HOME"]: o1 = price
                                                elif type_str in ["X", "DRAW"]: oX = price
                                                elif type_str in ["2", "AWAY"]: o2 = price

                    if home and away:
                        raw_parsed.append({
                            "match_id": str(item.get("id") or item.get("eventId") or ""),
                            "home_team": home, "away_team": away,
                            "competition": str(item.get("tournamentName") or item.get("categoryName") or "Unknown"),
                            "home_odds": o1, "draw_odds": oX, "away_odds": o2,
                            "sport": resolve_sport_name(item.get("sportId") or item.get("sportName")),
                            "market_type": "1X2" if oX is not None else "2WAY",
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
                        "sport": resolve_sport_name(item.get("sport_id") or item.get("sport_name")),
                        "market_type": "1X2" if item.get("neutral_odd") else "2WAY",
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
                        sport_name = resolve_sport_name(item.get("family", {}).get("name") if isinstance(item.get("family"), dict) else item.get("sport"))
                        raw_parsed.append({
                            "match_id": str(item.get("id") or ""),
                            "home_team": home, "away_team": away,
                            "competition": str(item.get("league", {}).get("name") if isinstance(item.get("league"), dict) else "Unknown"),
                            "home_odds": o1, "draw_odds": oX, "away_odds": o2,
                            "sport": sport_name, "market_type": "1X2" if oX is not None else "2WAY",
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
                                        "sport": resolve_sport_name(event.get("sportId") or cat.get("name")),
                                        "market_type": "1X2" if oX is not None else "2WAY",
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
                                                if not ("1X2" in m_name or "3-WAY" in m_name or "WINNER" in m_name or str(market.get("id")) == "1"): continue

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
                                "sport": resolve_sport_name(match.get("sportId") or match.get("sportName")),
                                "market_type": "1X2" if oX is not None else "2WAY",
                                "bookmaker_id": bookmaker_id, "timestamp": ts, "latency_ms": latency_ms
                            })

        # 7. SPORTYBET & SPORTPESA
        elif parser_type == "sportybet":
            data_obj = payload.get("data", {}) if isinstance(payload, dict) else {}
            tournaments = data_obj.get("tournaments", []) or data_obj.get("events", []) if isinstance(data_obj, dict) else []
            if not tournaments and isinstance(payload, list):
                tournaments = payload

            for tourney in tournaments:
                if isinstance(tourney, dict):
                    events = tourney.get("events", []) if isinstance(tourney, dict) else [tourney]
                    for item in events:
                        if isinstance(item, dict):
                            home = extract_team_name(item.get("homeTeamName") or item.get("homeTeam") or item.get("home"))
                            away = extract_team_name(item.get("awayTeamName") or item.get("awayTeam") or item.get("away"))
                            o1, oX, o2 = None, None, None
                            markets = item.get("markets", []) or item.get("marketsList", [])
                            if isinstance(markets, list):
                                for market in markets:
                                    if isinstance(market, dict) and (str(market.get("id")) in ["1", "10", "18", "29"] or any(k in str(market.get("name", "")).upper() for k in ["1X2", "3-WAY", "WINNER"])):
                                        outcomes = market.get("outcomes", []) or market.get("selections", [])
                                        if isinstance(outcomes, list):
                                            for outcome in outcomes:
                                                if isinstance(outcome, dict):
                                                    desc = str(outcome.get("desc") or outcome.get("outcomeName") or outcome.get("name") or "").upper()
                                                    price = safe_float(outcome.get("odds") or outcome.get("price") or outcome.get("value"))
                                                    if desc in ["1", "HOME"]: o1 = price
                                                    elif desc in ["X", "DRAW"]: oX = price
                                                    elif desc in ["2", "AWAY"]: o2 = price

                            raw_parsed.append({
                                "match_id": str(item.get("eventId") or item.get("id") or item.get("gameId") or ""),
                                "home_team": home, "away_team": away,
                                "competition": str(tourney.get("name") or item.get("competition") or "Unknown"),
                                "home_odds": o1, "draw_odds": oX, "away_odds": o2,
                                "sport": resolve_sport_name(item.get("sport", {}).get("id") if isinstance(item.get("sport"), dict) else item.get("sportId")),
                                "market_type": "1X2" if oX is not None else "2WAY",
                                "bookmaker_id": bookmaker_id, "timestamp": ts, "latency_ms": latency_ms
                            })

        # 8. RECURSIVE GENERIC FALLBACK
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
        elif len(raw_parsed) > 0 and NETWORK_RECORDER:
            logger.warning(
                f"[{bookmaker_id.upper()}-VALIDATION-REJECT] Extracted {len(raw_parsed)} items, "
                f"but 0 passed validate_match(). Sample: {raw_parsed[:1]}"
            )

    except Exception as e:
        logger.error(f"[{bookmaker_id}] Parser Exception ({type(e).__name__}): {repr(e)}")

    return matches


# -------------------------------------------------------------------
# HARDENED HTTP FETCHER WITH INDIVIDUAL BOOKMAKER TIMEOUTS
# -------------------------------------------------------------------

async def fetch_http_api(session: AsyncSession, bookmaker_id: str, config: dict, retries: int = 1) -> List[Dict[str, Any]]:
    url = config["url"]
    headers = get_dynamic_headers(url)
    verify_ssl = config.get("verify_ssl", True)
    timeout = config.get("timeout", 8)

    async with HTTP_SEMAPHORE:
        for attempt in range(retries):
            try:
                res = await session.get(url, headers=headers, impersonate="chrome", timeout=timeout, verify=verify_ssl)

                if res.status_code in [404, 401, 403, 502]:
                    logger.warning(f"[{bookmaker_id.upper()}] Returned HTTP {res.status_code}")
                    return []

                if res.status_code in [200, 203]:
                    try:
                        data = res.json()
                        return parse_raw_payload(bookmaker_id, data)
                    except Exception:
                        logger.warning(f"[{bookmaker_id.upper()}] Non-JSON response")
                        return []
            except Exception as e:
                err_msg = str(e)
                if "11001" in err_msg or "resolve" in err_msg.lower() or "curl: (6)" in err_msg:
                    logger.error(f"[{bookmaker_id.upper()}] DNS Lookup Failed for host")
                    return []
                logger.error(f"[{bookmaker_id.upper()}] Fetch Exception: {err_msg}")
    return []


# -------------------------------------------------------------------
# ADVANCED PLAYWRIGHT INTERCEPTOR WITH SSL BYPASS & SPLIT TIMEOUTS
# -------------------------------------------------------------------

async def intercept_playwright_spa(browser: Browser, bookmaker_id: str, config: dict, discovery_timeout: float = 20.0) -> List[Dict[str, Any]]:
    url = config["url"]
    keywords = config.get("keywords", [])
    bm_label = bookmaker_id.upper()
    captured_payloads = []
    all_matches = []

    # Forensic Stores
    redirect_chain = []
    requests_log = []
    responses_log = []
    websockets_log = []
    json_candidates = []

    async with PLAYWRIGHT_SEMAPHORE:
        start_t = time.perf_counter()
        context = None
        page = None
        try:
            context = await browser.new_context(
                ignore_https_errors=True,
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

            # 1. Track Timestamped Frame Navigations
            page.on("framenavigated", lambda frame: redirect_chain.append({"time": round(time.perf_counter() - start_t, 2), "url": frame.url}) if (frame == page.main_frame) else None)

            # 2. Track WebSockets
            page.on("websocket", lambda ws: websockets_log.append(ws.url))

            # 3. Route Interceptor
            async def handle_route(route):
                req = route.request
                if req.resource_type in ("xhr", "fetch"):
                    requests_log.append({
                        "url": req.url,
                        "method": req.method,
                        "resource": req.resource_type,
                        "frame": "main" if route.request.frame == page.main_frame else "subframe"
                    })
                await route.continue_()

            await page.route("**/*", handle_route)

            # 4. Filter Noise BEFORE Structural Scoring
            async def handle_response(response):
                if response.status in [200, 203]:
                    res_url = response.url.lower()
                    content_type = response.headers.get("content-type", "")

                    if any(x in res_url for x in IGNORE_JSON_KEYWORDS):
                        return

                    try:
                        content_length = int(response.headers.get("content-length", 0)) or len(await response.body())
                    except Exception:
                        content_length = 0

                    if not any(ext in res_url for ext in [".css", ".png", ".jpg", ".jpeg", ".svg", ".woff", ".ico", ".gif"]):
                        responses_log.append({"url": response.url, "status": response.status, "content_type": content_type, "size_bytes": content_length})

                    if "json" in content_type:
                        try:
                            json_data = await response.json()
                            if json_data:
                                score = score_sportsbook_payload(json_data)
                                matches_kw = any(kw.lower() in res_url for kw in keywords)

                                json_preview_str = json.dumps(json_data)[:500] if isinstance(json_data, (dict, list)) else str(json_data)[:500]
                                top_keys = list(json_data.keys())[:15] if isinstance(json_data, dict) else []

                                candidate_entry = {
                                    "url": response.url,
                                    "score": score,
                                    "size_bytes": content_length,
                                    "top_level_keys": top_keys,
                                    "preview": json_preview_str
                                }
                                json_candidates.append(candidate_entry)

                                if score > 15 or matches_kw:
                                    captured_payloads.append((response.url, json_data))
                                    if score > 25 and not matches_kw:
                                        update_endpoint_cache(bookmaker_id, response.url, score)

                                    if NETWORK_RECORDER:
                                        logger.info(f"[{bm_label}-PAYLOAD-DISCOVERED] URL: {response.url} | Score: {score}")
                        except Exception:
                            pass

            page.on("response", handle_response)
            logger.info(f"[{bm_label}-INTERCEPTOR] Navigating to target: {url}...")

            # 5. Split Navigation Execution: wait_until="commit" with 30s Timeout
            nav_start = time.perf_counter()
            try:
                await page.goto(url, wait_until="commit", timeout=30000)
                nav_duration = time.perf_counter() - nav_start
                logger.info(f"[{bm_label}-LANDED] Target: {url} | Final URL: {page.url} | Nav Completed in {nav_duration:.2f}s")

                # 6. Separate Discovery Polling Timeout Loop (20s)
                deadline = time.time() + discovery_timeout
                while time.time() < deadline:
                    if len(captured_payloads) > 0:
                        logger.info(f"[{bm_label}-EARLY-EXIT] Discovered {len(captured_payloads)} high-scoring payloads.")
                        break

                    await page.mouse.wheel(0, 800)
                    await asyncio.sleep(1.0)

            except Exception as e:
                logger.warning(f"[{bm_label}-NAV-WARNING] Navigation incomplete or timed out: {repr(e)}")

            # 7. DOM State & SSR Hydration Parsing Fallback
            html_content = ""
            page_title = ""
            try:
                page_title = await page.title()
                html_content = await page.content()

                if len(captured_payloads) == 0:
                    ssr_payloads = extract_ssr_hydration_json(html_content)
                    for ssr_data in ssr_payloads:
                        captured_payloads.append((page.url, ssr_data))
                        logger.info(f"[{bm_label}-SSR-EXTRACTED] Parsed inline hydration payload from DOM state.")
            except Exception:
                pass

            # 8. Parse Matches
            latency_ms = int((time.perf_counter() - start_t) * 1000)
            for idx, (res_url, payload) in enumerate(captured_payloads, 1):
                parsed = parse_raw_payload(bookmaker_id, payload, latency_ms=latency_ms)
                all_matches.extend(parsed)

            unique_matches = list({f"{m['bookmaker_id']}_{m['match_id']}": m for m in all_matches if isinstance(m, dict) and m.get("match_id")}.values()) if all_matches else []
            logger.info(f"[{bm_label}-SUMMARY] Captured {len(captured_payloads)} total payloads, parsed {len(unique_matches)} unique matches.")

            # 9. Forensic Artifact Dump (Only executed when SAVE_DIAGNOSTICS=true on 0 matches)
            if SAVE_DIAGNOSTICS and len(unique_matches) == 0:
                bm_dir = os.path.join(DIAGNOSTICS_DIR, bookmaker_id)
                os.makedirs(bm_dir, exist_ok=True)

                diagnostic_artifact = {
                    "bookmaker": bookmaker_id,
                    "target_url": url,
                    "final_url": page.url if page else "N/A",
                    "title": page_title,
                    "redirect_chain": redirect_chain,
                    "requests": requests_log,
                    "responses": responses_log,
                    "json_candidates": json_candidates,
                    "websockets": websockets_log,
                    "html_hydration_flags": {
                        "has_next_data": "__NEXT_DATA__" in html_content,
                        "has_nuxt": "__NUXT__" in html_content,
                        "has_initial_state": "__INITIAL_STATE__" in html_content,
                        "has_apollo_state": "__APOLLO_STATE__" in html_content,
                    }
                }

                with open(os.path.join(bm_dir, "report.json"), "w", encoding="utf-8") as f:
                    json.dump(diagnostic_artifact, f, indent=2)

                # Save compressed HTML page
                if html_content:
                    with open(os.path.join(bm_dir, "page.html.gz"), "wb") as f:
                        f.write(zlib.compress(html_content.encode("utf-8")))

                if page:
                    await page.screenshot(path=os.path.join(bm_dir, "screenshot.png"), full_page=True)

            return unique_matches

        except Exception as e:
            logger.error(f"[{bm_label}-INTERCEPTOR] Critical Error ({type(e).__name__}): {repr(e)}")
        finally:
            if page:
                try:
                    await page.close()
                except Exception:
                    pass
            if context:
                try:
                    await context.close()
                except Exception:
                    pass

    return []


# -------------------------------------------------------------------
# DISPATCHER MASTER SCANNER LOOP WITH DISCOVERED ENDPOINT ACCELERATION
# -------------------------------------------------------------------

async def scrape_all_sportsbooks() -> Dict[str, Any]:
    all_matches = []
    cache = load_endpoint_cache()

    # 1. Direct REST Targets (Static Registry + Discovered Dynamic Endpoints)
    http_targets = {}
    playwright_targets = {}

    for bm, cfg in BOOKMAKER_REGISTRY.items():
        if cfg["platform"] == "public_rest":
            http_targets[bm] = cfg
        elif bm in cache and cache[bm].get("endpoint"):
            cached_url = cache[bm]["endpoint"]
            http_targets[bm] = {
                "platform": "public_rest",
                "url": cached_url,
                "parser": cfg.get("parser", "generic"),
                "timeout": 10
            }
            logger.info(f"[{bm.upper()}-ACCELERATED] Using discovered endpoint from cache: {cached_url}")
        else:
            playwright_targets[bm] = cfg

    # Execute Fast REST Fetching
    async with AsyncSession() as session:
        http_tasks = [fetch_http_api(session, bm, cfg) for bm, cfg in http_targets.items()]
        http_results = await asyncio.gather(*http_tasks, return_exceptions=True)
        for idx, (bm, res) in enumerate(zip(http_targets.keys(), http_results)):
            if isinstance(res, list) and len(res) > 0:
                all_matches.extend([x for x in res if isinstance(x, dict)])
            elif bm in cache and BOOKMAKER_REGISTRY[bm]["platform"] == "playwright_spa":
                logger.warning(f"[{bm.upper()}-CACHE-EXPIRED] Cached endpoint failed. Re-enabling Playwright Discovery engine.")
                playwright_targets[bm] = BOOKMAKER_REGISTRY[bm]

    # Execute Playwright Interceptors with Ignore Certificate Error Flag
    if playwright_targets:
        try:
            async with async_playwright() as p:
                browser = await p.chromium.launch(
                    headless=True,
                    args=[
                        "--no-sandbox",
                        "--disable-setuid-sandbox",
                        "--disable-blink-features=AutomationControlled",
                        "--disable-dev-shm-usage",
                        "--ignore-certificate-errors"
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

    # Calculate Cross-Bookmaker Arbitrage
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
