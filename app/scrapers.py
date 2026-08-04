#!/usr/bin/env python3
"""
Scraper: Robust Odds Fetcher + Arbitrage Engine
Improvements:
1) Deeper WebSocket integration for real-time odds
2) Rate limiting with exponential backoff + optional proxies
3) Safer, extensible parser with a stronger fallback
4) Externalized registry via bookmakers.yaml (overrides)
5) Improved event fingerprinting / deduplication
6) Enhanced Playwright waiting, selectors, and load-more handling
7) Data normalization adding basic aliases
8) Safer asyncio.gather with per-task error handling
"""

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

# Optional YAML for external config
try:
    import yaml  # type: ignore
except Exception:
    yaml = None

# HTTP client
from curl_cffi.requests import AsyncSession

# Logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("scrapers")

# -------------------------------------------------------------------
# Feature Flags & Cache Configuration
# -------------------------------------------------------------------
NETWORK_RECORDER = os.getenv("NETWORK_RECORDER", "false").lower() == "true"
SAVE_DIAGNOSTICS = os.getenv("SAVE_DIAGNOSTICS", "true").lower() == "true"
DIAGNOSTICS_DIR = "diagnostics"
CACHE_FILE = "endpoints_cache.json"
CACHE_TTL_SECONDS = 3600 * 24  # 24 hours TTL
PROXY_ENV = os.getenv("SCRAPER_PROXY")  # Optional: global proxy (e.g., http://user:pass@host:port)

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

# User-Agent & headers
DESKTOP_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

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

# Sports map
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

# WebSocket odds state: bookmaker_id -> event_key -> {home, draw, away}
WEB_SOCKET_ODDS_STATE: Dict[str, Dict[str, Dict[str, Optional[float]]]] = {}

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
                        try:
                            last_verified_ts = time.mktime(
                                time.strptime(entry["last_verified"], "%Y-%m-%dT%H:%M:%SZ")
                            )
                            if current_time - last_verified_ts < CACHE_TTL_SECONDS:
                                cleaned_cache[bm_id] = entry
                        except ValueError:
                            logger.warning(f"Invalid last_verified format for {bm_id}: {entry.get('last_verified')}")
                return cleaned_cache
        except Exception as e:
            logger.error(f"Failed to load or clean endpoint cache: {repr(e)}")
            pass
    return {}

def load_external_registry(base_registry: Dict[str, Any]) -> Dict[str, Any]:
    """Load external registry overrides from bookmakers.yaml if available."""
    reg = dict(base_registry)
    if yaml is not None and os.path.exists("bookmakers.yaml"):
        try:
            with open("bookmakers.yaml", "r", encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
                if isinstance(data, dict):
                    reg.update(data)
                    logger.info("Loaded external bookmaker registry from bookmakers.yaml")
        except Exception as e:
            logger.error(f"Failed to load bookmakers.yaml: {repr(e)}")
    return reg

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

def normalize_team_name(name: str) -> str:
    # Basic normalization + simple alias handling
    if not name:
        return ""
    s = name.strip().lower()
    aliases = {
        "man utd": "manchester united",
        "man united": "manchester united",
        "man city": "manchester city",
    }
    s = aliases.get(s, s)
    return s

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
    keywords = [
        "home_team", "away_team", "competitors", "eventnames", "odds",
        "markets", "outcomes", "1x2", "coef", "runners", "price", "value"
    ]
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
        home_name = valid_matches[0].get("home_team", "").strip()
        away_name = valid_matches[0].get("away_team", "").strip()
        competition = valid_matches[0].get("competition", "Unknown")
        best_home = max([m for m in valid_matches if m.get("home_odds") is not None], key=lambda x: x.get("home_odds") or 0, default=None)
        best_away = max([m for m in valid_matches if m.get("away_odds") is not None], key=lambda x: x.get("away_odds") or 0, default=None)
        o1 = best_home.get("home_odds") if best_home else None
        o2 = best_away.get("away_odds") if best_away else None
        if not o1 or not o2:
            continue

        # 1) 2-WAY ARBITRAGE
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
        # 2) 3-WAY ARBITRAGE
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
    # Improve: robust dedup across matches
    # Use a fingerprint map to deduplicate
    deduped: Dict[str, Dict[str, Any]] = {}
    for m in opportunities:
        k = f"{m['sport']}|{m['competition']}|{m['event']}"
        if k not in deduped or m.get("profit_margin_pct", 0) > deduped[k].get("profit_margin_pct", 0):
            deduped[k] = m
    return list(deduped.values())

# -------------------------------------------------------------------
# BOOKMAKER REGISTRY (Minimal default; can be overridden by bookmakers.yaml)
# -------------------------------------------------------------------
DEFAULT_BOOKMAKER_REGISTRY: Dict[str, Dict[str, Any]] = {
    # Minimal example; you can override/add more via bookmakers.yaml
    "betika": {"platform": "public_rest", "url": "https://api.betika.com/v1/uo/matches?limit=100&sub_type=prematch", "parser": "betika", "timeout": 8},
    "meridianbet": {"platform": "public_rest", "url": "https://online.meridianbet.co.tz/api/v2/events/standard", "parser": "meridianbet", "timeout": 10},
    "1xcorp-demo": {"platform": "public_rest", "url": "https://example.com/api/v1/matches", "parser": "1xcorp", "timeout": 8},
}

# Load external overrides (if bookmakers.yaml exists)
BOOKMAKER_REGISTRY = load_external_registry(DEFAULT_BOOKMAKER_REGISTRY)
BOOKMAKER_MAP = {bm: None for bm in BOOKMAKER_REGISTRY.keys()}

# -------------------------------------------------------------------
# PARSER ENGINE
# -------------------------------------------------------------------
def parse_raw_payload(bookmaker_id: str, payload: Any, latency_ms: int = 0) -> List[Dict[str, Any]]:
    ts = int(time.time())
    config = BOOKMAKER_REGISTRY.get(bookmaker_id, {})
    parser_type = config.get("parser", "generic")
    raw_parsed: List[Dict[str, Any]] = []

    try:
        if not isinstance(payload, (dict, list)):
            return []
        if isinstance(payload, dict) and any(str(k).startswith("-1") for k in payload.keys()):
            return []

        # 1) 1XCORP Clone
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
                if not isinstance(item, dict):
                    continue
                home = normalize_team_name(item.get("O1") or item.get("HT") or item.get("HomeTeam"))
                away = normalize_team_name(item.get("O2") or item.get("AT") or item.get("AwayTeam"))
                if not home or not away:
                    raw_name = str(item.get("N") or item.get("Name") or "")
                    if " - " in raw_name:
                        parts = raw_name.split(" - ", 1)
                        home, away = parts[0].strip(), parts[1].strip()
                raw_sport_id = item.get("SI") or item.get("SportId") or item.get("SN")
                detected_sport = resolve_sport_name(raw_sport_id)
                competition = str(item.get("LE") or item.get("League") or item.get("L") or "Unknown")
                o1, oX, o2 = None, None, None
                raw_e = item.get("E") or item.get("Events") or item.get("Markets") or []
                flat_outcomes: List[Any] = []
                if isinstance(raw_e, list):
                    for element in raw_e:
                        if isinstance(element, list):
                            flat_outcomes.extend(element)
                        elif isinstance(element, dict):
                            flat_outcomes.append(element)
                for outcome in flat_outcomes:
                    if not isinstance(outcome, dict):
                        continue
                    t = outcome.get("T") or outcome.get("Type")
                    price = safe_float(outcome.get("C") or outcome.get("Coef") or outcome.get("Price"))
                    if t in [1, "1"]:
                        o1 = price
                    elif t in [2, "2", "X"]:
                        oX = price
                    elif t in [3, "3", "2"]:
                        o2 = price
                if home and away:
                    raw_parsed.append({
                        "match_id": str(item.get("I") or item.get("ID") or item.get("Ci") or ""),
                        "home_team": home, "away_team": away,
                        "competition": competition,
                        "home_odds": o1, "draw_odds": oX, "away_odds": o2,
                        "sport": detected_sport, "market_type": "1X2" if oX is not None else "2WAY",
                        "bookmaker_id": bookmaker_id, "timestamp": ts, "latency_ms": latency_ms
                    })

        # 2) MeridianBet
        elif parser_type == "meridianbet":
            events_list: List[Any] = []
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
                if not isinstance(item, dict):
                    continue
                home = normalize_team_name(item.get("home") or item.get("homeTeam") or item.get("team1"))
                away = normalize_team_name(item
