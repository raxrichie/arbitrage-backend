#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Robust Odds Scraper + Arbitrage Engine

- Supports Deploy-as-Web-Service (Render) or Background Worker modes.
- External registry overrides via bookmakers.yaml.
- Real-time WebSocket odds integration (stateful per bookmaker via CDP).
- Exponential backoff and per-bookmaker proxy support for REST endpoints.
- Extensible parser with strong recursive fallbacks.
- Event fingerprinting and cross-bookmaker deduplication.
- Enhanced Playwright handling (selectors, network idle, load-more).
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
except ImportError:
    yaml = None

from curl_cffi.requests import AsyncSession

# Optional FastAPI for web mode
FASTAPI_AVAILABLE = False
try:
    from fastapi import FastAPI
    FASTAPI_AVAILABLE = True
except ImportError:
    pass

# Optional uvicorn for serving web mode
UVICORN_AVAILABLE = False
try:
    import uvicorn
    UVICORN_AVAILABLE = True
except ImportError:
    pass

# Logging Setup
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
SCRAPER_PROXY = os.getenv("SCRAPER_PROXY")  # Optional global proxy
SCRAPER_INTERVAL = int(os.getenv("SCRAPER_INTERVAL", "600"))  # 10 minutes default
SCRAPER_MODE = os.getenv("SCRAPER_MODE", "worker").lower()  # web or worker

if SAVE_DIAGNOSTICS and not os.path.exists(DIAGNOSTICS_DIR):
    os.makedirs(DIAGNOSTICS_DIR, exist_ok=True)

IGNORE_JSON_KEYWORDS = (
    "growthbook", "analytics", "geo-location", "config", "translations",
    "feature", "consent", "cookies", "telemetry", "google-analytics", "facebook"
)

# Stealth Browser Initialization
try:
    from patchright.async_api import Browser, async_playwright
    logger.info("Using patchright for browser automation (stealth enabled).")
except ImportError:
    from playwright.async_api import Browser, async_playwright
    logger.warning("patchright not available; falling back to plain playwright.")

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

WEB_SOCKET_ODDS_STATE: Dict[str, Dict[str, Dict[str, Optional[float]]]] = {}


# -------------------------------------------------------------------
# ENDPOINT DISCOVERY & REGISTRY
# -------------------------------------------------------------------
def load_endpoint_cache() -> Dict[str, Any]:
    if os.path.exists(CACHE_FILE):
        try:
            with open(CACHE_FILE, "r", encoding="utf-8") as f:
                cache_data = json.load(f)
            now = time.time()
            cleaned = {}
            for k, v in cache_data.items():
                ts = v.get("last_verified")
                if ts:
                    try:
                        last = time.mktime(time.strptime(ts, "%Y-%m-%dT%H:%M:%SZ"))
                        if now - last < CACHE_TTL_SECONDS:
                            cleaned[k] = v
                    except ValueError:
                        continue
            return cleaned
        except Exception as e:
            logger.error(f"Failed to load endpoint cache: {repr(e)}")
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
        logger.info(f"[{bookmaker_id.upper()}-CACHE-UPDATED] Saved endpoint: {endpoint_url} (score={score})")
    except Exception as e:
        logger.error(f"[{bookmaker_id.upper()}-CACHE-ERROR] Failed to save endpoint cache: {repr(e)}")


def load_external_registry(base_registry: Dict[str, Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    reg = dict(base_registry)
    if yaml is None or not os.path.exists("bookmakers.yaml"):
        return reg
    try:
        with open("bookmakers.yaml", "r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
            if isinstance(data, dict):
                reg.update(data)
                logger.info("Merged external bookmakers.yaml overrides into registry.")
            return reg
    except Exception as e:
        logger.error(f"Failed to load bookmakers.yaml: {repr(e)}")
        return reg


# -------------------------------------------------------------------
# UTILITY & NORMALIZATION
# -------------------------------------------------------------------
def normalize_team_name(name: Any) -> str:
    if not name:
        return ""
    s = str(name).strip().lower()
    aliases = {
        "man utd": "manchester united",
        "man united": "manchester united",
        "man city": "manchester city",
        "fc barcelona": "barcelona",
        "fc madrid": "real madrid",
        "real madrid": "real madrid",
    }
    return aliases.get(s, s)


def extract_team_name(val: Any) -> str:
    if isinstance(val, dict):
        return str(val.get("name") or val.get("nameDefault") or val.get("title") or "").strip()
    return str(val or "").strip()


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
        norm = normalize_team_name(name)
        words = [w for w in "".join(c if c.isalnum() else " " for c in norm.lower()).split() if len(w) > 1]
        meaningful = [w for w in words if w not in ignored_words]
        return sorted(meaningful) if meaningful else sorted(words)

    home_tokens = tokenize(home)
    away_tokens = tokenize(away)

    if not home_tokens or not away_tokens:
        return f"{sport}_{home.lower()[:8]}_vs_{away.lower()[:8]}"

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
        f = float(val)
        return f if f > 1.01 else default
    except (ValueError, TypeError):
        return default


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


def deduplicate_matches(all_matches: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    if not all_matches:
        return []
    seen: Dict[str, Dict[str, Any]] = {}
    for m in all_matches:
        if not isinstance(m, dict):
            continue
        key = f"{m.get('bookmaker_id','')}|{m.get('match_id','')}"
        if key not in seen:
            seen[key] = m
        else:
            existing = seen[key]
            if (m.get("home_odds") is not None) and (existing.get("home_odds") is None):
                seen[key] = m
            elif (m.get("away_odds") is not None) and (existing.get("away_odds") is None):
                seen[key] = m
    return list(seen.values())


def extract_ssr_hydration_json(html_content: str) -> List[Dict[str, Any]]:
    found = []
    patterns = [
        r'<script[^>]*id="__NEXT_DATA__"[^>]*>(.*?)</script>',
        r'<script[^>]*id="__NUXT__"[^>]*>(.*?)</script>',
        r'window\.__INITIAL_STATE__\s*=\s*({.*?});',
        r'window\.__PRELOADED_STATE__\s*=\s*({.*?});',
        r'window\.__APOLLO_STATE__\s*=\s*({.*?});'
    ]
    for pat in patterns:
        for match in re.findall(pat, html_content, re.DOTALL):
            try:
                data = json.loads(match.strip())
                if isinstance(data, (dict, list)):
                    found.append(data)
            except Exception:
                pass
    return found


def find_events_recursive(obj: Any, depth: int = 0) -> List[Dict[str, Any]]:
    if depth > 4:
        return []
    if isinstance(obj, list):
        if obj and isinstance(obj[0], dict):
            sample = obj[0]
            if any(k in sample for k in ["home_team", "homeTeam", "competitors", "eventNames", "O1", "teams", "eName", "games", "matchId"]):
                return [x for x in obj if isinstance(x, dict)]
        events = []
        for item in obj[:10]:
            if isinstance(item, (dict, list)):
                events.extend(find_events_recursive(item, depth + 1))
        return events
    elif isinstance(obj, dict):
        for k in ["events", "matches", "games", "fixtures", "items", "groupList", "matchVoList", "data", "sportsTree", "results"]:
            if k in obj:
                v = obj[k]
                if isinstance(v, list):
                    return [x for x in v if isinstance(x, dict)]
                if isinstance(v, dict):
                    found = find_events_recursive(v, depth + 1)
                    if found:
                        return found
        for v in obj.values():
            if isinstance(v, (dict, list)):
                found = find_events_recursive(v, depth + 1)
                if found:
                    return found
    return []


# -------------------------------------------------------------------
# CROSS-BOOKMAKER ARBITRAGE CALCULATOR
# -------------------------------------------------------------------
def find_arbitrage_opportunities(all_matches: List[Dict[str, Any]], bankroll: float = 100000.0) -> List[Dict[str, Any]]:
    grouped: Dict[str, List[Dict[str, Any]]] = {}
    for m in all_matches:
        if not isinstance(m, dict):
            continue
        home = str(m.get("home_team", "")).strip()
        away = str(m.get("away_team", "")).strip()
        sport = str(m.get("sport", "soccer"))
        if not home or not away or home == away:
            continue
        key = generate_event_fingerprint(home, away, sport)
        grouped.setdefault(key, []).append(m)

    opportunities: List[Dict[str, Any]] = []
    for key, matches in grouped.items():
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

        if sport in ["tennis", "basketball", "volleyball", "mma", "table_tennis", "baseball", "darts", "handball"]:
            if str(best_home.get("bookmaker_id")) == str(best_away.get("bookmaker_id")):
                continue
            arb_margin = (1.0 / o1) + (1.0 / o2)
            if 0.85 < arb_margin < 1.0:
                profit_pct = round((1.0 - arb_margin) * 100, 2)
                stake1 = round(bankroll / (o1 * arb_margin), -1)
                stake2 = round(bankroll / (o2 * arb_margin), -1)
                total_invested = stake1 + stake2
                payout1 = round(stake1 * o1, 2)
                payout2 = round(stake2 * o2, 2)
                min_payout = min(payout1, payout2)
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
                        {"outcome": f"1 ({home_name})", "bookmaker": str(best_home.get("bookmaker_id", "")).upper(), "odds": o1, "recommended_stake": stake1, "expected_payout": payout1},
                        {"outcome": f"2 ({away_name})", "bookmaker": str(best_away.get("bookmaker_id", "")).upper(), "odds": o2, "recommended_stake": stake2, "expected_payout": payout2},
                    ],
                })
        else:
            best_draw = max([m for m in valid_matches if m.get("draw_odds") is not None], key=lambda x: x.get("draw_odds") or 0, default=None)
            if not best_draw:
                continue
            oX = best_draw.get("draw_odds")
            if not oX:
                continue
            used_bookies = {
                str(best_home.get("bookmaker_id")),
                str(best_draw.get("bookmaker_id")),
                str(best_away.get("bookmaker_id")),
            }
            if len(used_bookies) < 2:
                continue
            arb_margin = (1.0 / o1) + (1.0 / oX) + (1.0 / o2)
            if 0.85 < arb_margin < 1.0:
                profit_pct = round((1.0 - arb_margin) * 100, 2)
                stake1 = round(bankroll / (o1 * arb_margin), -1)
                stakeX = round(bankroll / (oX * arb_margin), -1)
                stake2 = round(bankroll / (o2 * arb_margin), -1)
                total_invested = stake1 + stakeX + stake2
                payout1 = round(stake1 * o1, 2)
                payoutX = round(stakeX * oX, 2)
                payout2 = round(stake2 * o2, 2)
                min_payout = min(payout1, payoutX, payout2)
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
                        {"outcome": f"1 ({home_name})", "bookmaker": str(best_home.get("bookmaker_id", "")).upper(), "odds": o1, "recommended_stake": stake1, "expected_payout": payout1},
                        {"outcome": "X (Draw)", "bookmaker": str(best_draw.get("bookmaker_id", "")).upper(), "odds": oX, "recommended_stake": stakeX, "expected_payout": payoutX},
                        {"outcome": f"2 ({away_name})", "bookmaker": str(best_away.get("bookmaker_id", "")).upper(), "odds": o2, "recommended_stake": stake2, "expected_payout": payout2},
                    ],
                })

    deduped: Dict[str, Dict[str, Any]] = {}
    for o in opportunities:
        key = f"{o['sport']}|{o['competition']}|{o['event']}"
        if key not in deduped or o.get("profit_margin_pct", 0) > deduped[key].get("profit_margin_pct", 0):
            deduped[key] = o
    return list(deduped.values())


# -------------------------------------------------------------------
# EXTENDED 25+ BOOKMAKER REGISTRY
# -------------------------------------------------------------------
DEFAULT_BOOKMAKER_REGISTRY: Dict[str, Dict[str, Any]] = {
    # Direct Public REST APIs
    "betika": {"platform": "public_rest", "url": "https://api.betika.com/v1/uo/matches?limit=100&sub_type=prematch", "parser": "betika", "timeout": 8},
    "sportybet": {"platform": "public_rest", "url": "https://www.sportybet.com/api/tz/factsCenter/pcUpcomingEvents?pageSize=100&pageNum=1&option=1", "parser": "sportybet", "timeout": 8},
    "bangbet": {"platform": "public_rest", "url": "https://bet-api.bangbet.com/api/bet/match/listTop?country=tz", "parser": "bangbet", "timeout": 8},
    "leonbet": {"platform": "public_rest", "url": "https://leonbet.co.tz/api-2/betline/events/all?ctag=en-US", "parser": "leonbet", "timeout": 25},
    "premierbet": {"platform": "public_rest", "url": "https://sports-api.premierbet.co.tz/v1/events/highlights?country=TZ&group=g2&platform=desktop&locale=sw&limit=100", "parser": "premierbet", "timeout": 8},
    "wasafibet": {"platform": "public_rest", "url": "https://api.wasafibet.co.tz/v1/uo/matches?limit=100&sub_type=prematch", "parser": "betika", "timeout": 8},
    "betpawa": {"platform": "public_rest", "url": "https://www.betpawa.co.tz/api/sportsbook/v1/events?sportId=1&limit=100", "parser": "generic", "timeout": 10},

    # ALL 1XCORP CLONES (Direct REST Engine)
    "22bet": {"platform": "public_rest", "url": "https://22bet.co.tz/service-api/LiveFeed/Get1x2_VZip?count=50&lng=en_GB&gr=329&mode=4&country=181&partner=151", "parser": "1xcorp", "timeout": 10},
    "helabet": {"platform": "public_rest", "url": "https://helabet.co.tz/service-api/LiveFeed/Get1x2_VZip?count=50&lng=en&gr=329&mode=4&country=181&partner=237", "parser": "1xcorp", "timeout": 10},
    "betwinner": {"platform": "public_rest", "url": "https://betwinner.co.tz/service-api/LiveFeed/Get1x2_VZip?count=50&lng=en&gr=329&mode=4&country=181&partner=777", "parser": "1xcorp", "timeout": 10},
    "1xbet": {"platform": "public_rest", "url": "https://1xbet.co.tz/service-api/LiveFeed/Get1x2_VZip?count=50&lng=en&gr=329&mode=4&country=181&partner=1499", "parser": "1xcorp", "timeout": 10},
    "1xbit": {"platform": "public_rest", "url": "https://1xbit.com/service-api/LiveFeed/Get1x2_VZip?count=50&lng=en&gr=329&mode=4&country=181&partner=933", "parser": "1xcorp", "timeout": 10},
    "megapari": {"platform": "public_rest", "url": "https://megapari.com/service-api/LiveFeed/Get1x2_VZip?count=50&lng=en&gr=329&mode=4&country=181&partner=824", "parser": "1xcorp", "timeout": 10},
    "melbet": {"platform": "public_rest", "url": "https://melbet.co.tz/service-api/LiveFeed/Get1x2_VZip?count=50&lng=en&gr=329&mode=4&country=181&partner=1", "parser": "1xcorp", "timeout": 10},
    "mostbet": {"platform": "public_rest", "url": "https://mostbet.com/api/v1/events?sport_id=1&limit=50", "parser": "generic", "timeout": 10},

    # ALTENAR & SPORT-RADAR POWERED BOOKMAKERS
    "sokabet": {"platform": "public_rest", "url": "https://sb2frontend-altenar2.gammastack.com/api/Sportsbook/GetTopEvents?culture=en-GB&timezoneOffset=-180&integration=sokabet&numevents=50", "parser": "generic", "timeout": 10},
    "888bet": {"platform": "public_rest", "url": "https://888bet.tz/api/v1/sportsbook/highlights?sportId=1", "parser": "premierbet", "timeout": 10},
    "pmbet": {"platform": "public_rest", "url": "https://pmbet.co.tz/api/v1/events/highlights?limit=50", "parser": "generic", "timeout": 10},
    "winprincess": {"platform": "public_rest", "url": "https://winprincess.co.tz/api/v1/sportsbook/highlights", "parser": "generic", "timeout": 10},
    "mozzartbet": {"platform": "public_rest", "url": "https://www.mozzartbet.co.tz/backend/v1/events/highlights", "parser": "generic", "timeout": 10},
    "10bet": {"platform": "public_rest", "url": "https://10bet.co.tz/api/v1/sportsbook/highlights", "parser": "generic", "timeout": 10},

    # PLAYWRIGHT SPA INTERCEPTORS
    "sportpesa": {"platform": "playwright_spa", "url": "https://www.sportpesa.co.tz/en/sports-betting/football-1/", "keywords": ["/api/", "games", "upcoming", "highlights"], "parser": "sportybet"},
    "meridianbet": {"platform": "playwright_spa", "url": "https://meridianbet.co.tz/en/betting/football", "keywords": ["/api/", "/events/", "betsapi", "standard", "v2"], "parser": "meridianbet"},
    "mbet": {"platform": "playwright_spa", "url": "https://mbet.co.tz", "keywords": ["/api/", "sportsbook", "matches", "events"], "parser": "generic"},
    "1win": {"platform": "playwright_spa", "url": "https://1win.co.tz", "keywords": ["/api/", "sports", "football", "matches"], "parser": "generic"},
    "kingbet": {"platform": "playwright_spa", "url": "https://www.kingbet.co.tz/en/sportsbook/highlights", "keywords": ["redis_data", "home", "events", "sportsbook"], "parser": "generic"},
    "galsport": {"platform": "playwright_spa", "url": "https://gsb.co.tz/en/sportsbook/highlights", "keywords": ["/api/", "highlights", "events", "sportsbook", "get", "fixtures", "evapi"], "parser": "generic"},
    "parimatch": {"platform": "playwright_spa", "url": "https://parimatch.co.tz/en/football/prematch", "keywords": ["prematch", "sportsbook", "events", "line"], "parser": "generic"},
    "betway": {"platform": "playwright_spa", "url": "https://www.betway.co.tz", "keywords": ["highlights", "sportsapi", "event", "betbook"], "parser": "generic"},
}

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

        # 1) 1XCorp Clones
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

        # 2) MeridianBet
        elif parser_type == "meridianbet":
            events_list = []
            if isinstance(payload, dict):
                sports = payload.get("sports", []) or [payload]
                for sp in sports:
                    if isinstance(sp, dict):
                        cats = sp.get("categories", []) or [sp]
                        for cat in cats:
                            if isinstance(cat, dict):
                                tours = cat.get("tournaments", []) or [cat]
                                for tour in tours:
                                    if isinstance(tour, dict):
                                        evs = tour.get("events", [])
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
                            if not isinstance(game, dict):
                                continue
                            game_name = str(game.get("name") or game.get("code") or "").upper()
                            if "1X2" in game_name or "FINAL RESULT" in game_name or "WINNER" in game_name or game.get("isPrimary"):
                                selections = game.get("selections") or game.get("outcomes") or []
                                if isinstance(selections, list):
                                    for sel in selections:
                                        if isinstance(sel, dict):
                                            t = str(sel.get("type") or sel.get("name") or "").upper()
                                            price = safe_float(sel.get("price") or sel.get("odd") or sel.get("value"))
                                            if t in ["1", "HOME"]: o1 = price
                                            elif t in ["X", "DRAW"]: oX = price
                                            elif t in ["2", "AWAY"]: o2 = price
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

        # 3) Betika
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

        # 4) LeonBet
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
                        name = item.get("name") or ""
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
                                if "1X2" in m_name or "WINNER" in m_name or "1X2" in m_type or market.get("primary"):
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

        # 5) PremierBet
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

        # 6) BangBet
        elif parser_type == "bangbet":
            groups = payload.get("data", {}).get("groupList", []) if isinstance(payload, dict) and isinstance(payload.get("data"), dict) else []
            for group in groups:
                if not isinstance(group, dict):
                    continue
                match_list = group.get("matchVoList") or group.get("matchList") or []
                for match in match_list:
                    if not isinstance(match, dict):
                        continue
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
                                        if not ("1X2" in m_name or "3-WAY" in m_name or "WINNER" in m_name or str(market.get("id")) == "1"):
                                            continue
                                        outcomes = market.get("outcomes") or market.get("optionList") or market.get("options") or []
                                        if isinstance(outcomes, list):
                                            for idx, outcome in enumerate(outcomes):
                                                if isinstance(outcome, dict):
                                                    desc_upper = str(outcome.get("desc") or outcome.get("type") or outcome.get("name") or "").strip().upper()
                                                    raw_price = outcome.get("odds") or outcome.get("price") or outcome.get("val")
                                                    try:
                                                        raw_val = float(raw_price) if raw_price is not None else None
                                                    except (ValueError, TypeError):
                                                        raw_val = None
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

        # 7) SportyBet & SportPesa
        elif parser_type == "sportybet":
            data_obj = payload.get("data", {}) if isinstance(payload, dict) else {}
            tournaments = data_obj.get("tournaments", []) or data_obj.get("events", []) if isinstance(data_obj, dict) else []
            if not tournaments and isinstance(payload, list):
                tournaments = payload

            for tourney in tournaments:
                if not isinstance(tourney, dict):
                    continue
                events = tourney.get("events", []) if isinstance(tourney, dict) else [tourney]
                for item in events:
                    if not isinstance(item, dict):
                        continue
                    home = extract_team_name(item.get("homeTeamName") or item.get("homeTeam") or item.get("home"))
                    away = extract_team_name(item.get("awayTeamName") or item.get("awayTeam") or item.get("away"))
                    o1, oX, o2 = None, None, None
                    markets = item.get("markets", []) or item.get("marketsList", [])
                    if isinstance(markets, list):
                        for market in markets:
                            if isinstance(market, dict) and (
                                str(market.get("id")) in ["1", "10", "18", "29"]
                                or any(k in str(market.get("name", "")).upper() for k in ["1X2", "3-WAY", "WINNER"])
                            ):
                                outcomes = market.get("outcomes", []) or market.get("selections", [])
                                if isinstance(outcomes, list):
                                    for outcome in outcomes:
                                        if isinstance(outcome, dict):
                                            desc = str(outcome.get("desc") or outcome.get("outcomeName") or outcome.get("name"))
                                            price = safe_float(outcome.get("odds") or outcome.get("price") or outcome.get("value"))
                                            d = str(desc).upper()
                                            if d in ["1", "HOME"]: o1 = price
                                            elif d in ["X", "DRAW"]: oX = price
                                            elif d in ["2", "AWAY"]: o2 = price
                    if home and away:
                        raw_parsed.append({
                            "match_id": str(item.get("eventId") or item.get("id") or item.get("gameId") or ""),
                            "home_team": home, "away_team": away,
                            "competition": str(tourney.get("name") or item.get("competition") or "Unknown"),
                            "home_odds": o1, "draw_odds": oX, "away_odds": o2,
                            "sport": resolve_sport_name(item.get("sport", {}).get("id") if isinstance(item.get("sport"), dict) else item.get("sportId")),
                            "market_type": "1X2" if oX is not None else "2WAY",
                            "bookmaker_id": bookmaker_id, "timestamp": ts, "latency_ms": latency_ms
                        })

        # 8) Recursive Fallback
        else:
            events = find_events_recursive(payload)
            for item in events:
                if not isinstance(item, dict):
                    continue
                home = extract_team_name(item.get("homeTeam") or item.get("home_team") or item.get("homeName") or item.get("team1"))
                away = extract_team_name(item.get("awayTeam") or item.get("away_team") or item.get("awayName") or item.get("team2"))
                raw_sport = item.get("sportId") or item.get("sport") or item.get("sportName") or item.get("categoryName")
                detected_sport = resolve_sport_name(raw_sport)
                competition = str(item.get("league") or item.get("competition") or item.get("leagueName") or "Unknown")
                raw_odds = item.get("odds") if isinstance(item.get("odds"), dict) else {}
                o1 = safe_float(item.get("home_odds") or item.get("homeOdds") or item.get("odds1") or raw_odds.get("1"))
                oX = safe_float(item.get("draw_odds") or item.get("drawOdds") or item.get("oddsX") or raw_odds.get("X"))
                o2 = safe_float(item.get("away_odds") or item.get("awayOdds") or item.get("odds2") or raw_odds.get("2"))
                if home and away:
                    raw_parsed.append({
                        "match_id": str(item.get("id") or item.get("eventId") or item.get("match_id") or ""),
                        "home_team": home, "away_team": away,
                        "competition": competition,
                        "home_odds": o1, "draw_odds": oX, "away_odds": o2,
                        "sport": detected_sport, "market_type": "1X2" if oX is not None else "2WAY",
                        "bookmaker_id": bookmaker_id, "timestamp": ts, "latency_ms": latency_ms
                    })

        # Final Validation & Return for ALL Parser Paths
        matches = [m for m in raw_parsed if isinstance(m, dict) and validate_match(m)]
        if matches:
            counts = Counter(m["sport"] for m in matches)
            breakdown = ", ".join(f"{sp}: {cnt}" for sp, cnt in counts.items())
            logger.info(f"[{bookmaker_id.upper()}] Parsed {len(matches)} valid matches ({breakdown})")
        elif raw_parsed and NETWORK_RECORDER:
            logger.warning(
                f"[{bookmaker_id.upper()}-VALIDATION-REJECT] Extracted {len(raw_parsed)} items, "
                f"but 0 passed validation. Sample: {raw_parsed[:1]}"
            )
        return matches

    except Exception as e:
        logger.error(f"[{bookmaker_id}] Parser Exception ({type(e).__name__}): {repr(e)}")
        return []


# -------------------------------------------------------------------
# HARDENED HTTP FETCHER WITH EXP BACKOFF & PROXY
# -------------------------------------------------------------------
async def fetch_http_api(session: AsyncSession, bookmaker_id: str, config: dict, retries: int = 3) -> List[Dict[str, Any]]:
    url = config["url"]
    headers = get_dynamic_headers(url)
    verify_ssl = config.get("verify_ssl", True)
    timeout = config.get("timeout", 8)
    proxy = config.get("proxy") or (SCRAPER_PROXY or None)

    async with HTTP_SEMAPHORE:
        for attempt in range(1, retries + 1):
            try:
                kwargs = {
                    "headers": headers,
                    "impersonate": "chrome",
                    "timeout": timeout,
                    "verify": verify_ssl,
                }
                if proxy:
                    kwargs["proxy"] = proxy
                res = await session.get(url, **kwargs)
                code = getattr(res, "status_code", None) or getattr(res, "status", None)

                if code in (429, 503):
                    backoff = min(2 ** attempt, 60)
                    logger.warning(f"[{bookmaker_id.upper()}] HTTP {code} - backing off {backoff:.1f}s (attempt {attempt}/{retries})")
                    await asyncio.sleep(backoff)
                    continue
                if code in (404, 401, 403, 502):
                    logger.warning(f"[{bookmaker_id.upper()}] Returned HTTP {code}")
                    return []
                if code in (200, 203, 201):
                    try:
                        data = res.json()
                        return parse_raw_payload(bookmaker_id, data)
                    except Exception:
                        logger.warning(f"[{bookmaker_id.upper()}] Non-JSON response")
                        return []
            except Exception as e:
                err = str(e)
                if "11001" in err or "resolve" in err.lower() or "curl" in err.lower():
                    logger.error(f"[{bookmaker_id.upper()}] DNS Lookup Failed for host")
                    return []
                logger.error(f"[{bookmaker_id.upper()}] Fetch Exception: {err}")
        return []


# -------------------------------------------------------------------
# ADVANCED PLAYWRIGHT INTERCEPTOR
# -------------------------------------------------------------------
async def intercept_playwright_spa(browser: Browser, bookmaker_id: str, config: dict, discovery_timeout: float = 20.0) -> List[Dict[str, Any]]:
    url = config["url"]
    keywords = config.get("keywords", [])
    bm_label = bookmaker_id.upper()

    captured_payloads: List[tuple] = []
    all_matches: List[Dict[str, Any]] = []

    redirect_chain: List[Dict[str, Any]] = []
    requests_log: List[Dict[str, Any]] = []
    responses_log: List[Dict[str, Any]] = []
    websockets_log: List[str] = []
    json_candidates: List[Dict[str, Any]] = []

    WEB_SOCKET_ODDS_STATE.setdefault(bookmaker_id, {})

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

            # CDP Session for WebSocket Frames
            try:
                cdp = await page.context.new_cdp_session(page)
                await cdp.send("Network.enable")

                def on_ws_frame(event):
                    payload = event.get("response", {}).get("payloadData", "")
                    if payload and len(payload) > 50:
                        score = score_sportsbook_payload(payload)
                        if score > 10:
                            try:
                                data = json.loads(payload)
                                captured_payloads.append((page.url, data))
                                logger.info(f"[{bm_label}-WS-EXTRACTED] Intercepted live odds WebSocket frame.")
                            except Exception:
                                pass

                cdp.on("Network.webSocketFrameReceived", on_ws_frame)
            except Exception as e:
                logger.warning(f"[{bm_label}-WS] CDP WS listener unavailable: {repr(e)}")

            page.on("framenavigated", lambda frame: redirect_chain.append({"time": round(time.perf_counter() - start_t, 2), "url": frame.url}) if (frame == page.main_frame) else None)
            page.on("websocket", lambda ws: websockets_log.append(ws.url))

            async def handle_route(route):
                req = route.request
                if req.resource_type in ("xhr", "fetch"):
                    requests_log.append({"url": req.url, "method": req.method, "resource": req.resource_type, "frame": "main" if route.request.frame == page.main_frame else "subframe"})
                await route.continue_()
            await page.route("**/*", handle_route)

            async def handle_response(response):
                if response.status in (200, 203):
                    res_url = response.url.lower()
                    content_type = response.headers.get("content-type", "")
                    if any(k in res_url for k in IGNORE_JSON_KEYWORDS):
                        return
                    try:
                        content_length = int(response.headers.get("content-length", 0)) or len(await response.body())
                    except Exception:
                        content_length = 0
                    if not any(ext in res_url for ext in [".css", ".png", ".jpg", ".jpeg", ".svg", ".woff", ".ico", ".gif"]):
                        responses_log.append({"url": response.url, "status": response.status, "content_type": content_type, "size_bytes": content_length})
                    if any(ct in content_type for ct in ["json", "text/plain", "text/javascript"]):
                        try:
                            json_data = await response.json()
                            if json_data:
                                score = score_sportsbook_payload(json_data)
                                matches_kw = any(k.lower() in res_url for k in keywords)
                                json_preview = json.dumps(json_data)[:500] if isinstance(json_data, (dict, list)) else str(json_data)[:500]
                                top_keys = list(json_data.keys())[:15] if isinstance(json_data, dict) else []
                                candidate = {"url": response.url, "score": score, "size_bytes": content_length, "top_level_keys": top_keys, "preview": json_preview}
                                json_candidates.append(candidate)
                                if score > 15 or matches_kw:
                                    captured_payloads.append((response.url, json_data))
                                    if score > 25 and not matches_kw:
                                        update_endpoint_cache(bookmaker_id, response.url, score)
                                    if NETWORK_RECORDER:
                                        logger.info(f"[{bm_label}-PAYLOAD] URL: {response.url} | Score: {score}")
                        except Exception:
                            pass

            page.on("response", handle_response)

            logger.info(f"[{bm_label}-INTERCEPTOR] Navigating to target: {url}...")
            nav_start = time.perf_counter()
            try:
                await page.goto(url, wait_until="commit", timeout=30000)
                nav_duration = time.perf_counter() - nav_start
                logger.info(f"[{bm_label}-LANDED] Target: {url} | Final URL: {page.url} | Nav Completed in {nav_duration:.2f}s")

                deadline = time.time() + discovery_timeout
                while time.time() < deadline:
                    if len(captured_payloads) > 0:
                        logger.info(f"[{bm_label}-EARLY-EXIT] Discovered {len(captured_payloads)} high-scoring payloads.")
                        break

                    await page.mouse.wheel(0, 800)
                    await asyncio.sleep(1.0)

            except Exception as e:
                logger.warning(f"[{bm_label}-NAV-WARNING] Navigation incomplete or timed out: {repr(e)}")

            html_content = ""
            page_title = ""
            try:
                page_title = await page.title()
                html_content = await page.content()
                if not captured_payloads:
                    ssr = extract_ssr_hydration_json(html_content)
                    for s in ssr:
                        captured_payloads.append((page.url, s))
                        logger.info(f"[{bm_label}-SSR] Extracted hydration payload.")
            except Exception:
                pass

            latency_ms = int((time.perf_counter() - start_t) * 1000)
            for idx, (res_url, payload) in enumerate(captured_payloads, 1):
                parsed = parse_raw_payload(bookmaker_id, payload, latency_ms=latency_ms)
                all_matches.extend(parsed)

            unique_matches = deduplicate_matches(all_matches)
            logger.info(f"[{bm_label}-SUMMARY] Captured {len(captured_payloads)} payloads, parsed {len(unique_matches)} matches.")

        except Exception as e:
            logger.error(f"[{bm_label}-INTERCEPTOR] Navigation/parse error: {type(e).__name__}: {e}")

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

        return unique_matches


# -------------------------------------------------------------------
# SCRAPER MASTER LOOP
# -------------------------------------------------------------------
async def scrape_all_sportsbooks() -> Dict[str, Any]:
    all_matches: List[Dict[str, Any]] = []
    cache = load_endpoint_cache()

    http_targets: Dict[str, Dict[str, Any]] = {}
    playwright_targets: Dict[str, Dict[str, Any]] = {}

    for bm, cfg in BOOKMAKER_REGISTRY.items():
        if cfg.get("platform") == "public_rest":
            http_targets[bm] = cfg
        elif bm in cache and cache[bm].get("endpoint"):
            cached_url = cache[bm]["endpoint"]
            http_targets[bm] = {
                "platform": "public_rest",
                "url": cached_url,
                "parser": cfg.get("parser", "generic"),
                "timeout": 10,
                "proxy": cfg.get("proxy"),
            }
            logger.info(f"[{bm.upper()}-ACCELERATED] Using cached endpoint: {cached_url}")
        else:
            playwright_targets[bm] = cfg

    async with AsyncSession() as session:
        http_tasks = [fetch_http_api(session, bm, cfg) for bm, cfg in http_targets.items()]
        http_results = await asyncio.gather(*http_tasks, return_exceptions=True)
        for bm, res in zip(http_targets.keys(), http_results):
            if isinstance(res, list) and len(res) > 0:
                all_matches.extend([x for x in res if isinstance(x, dict)])
            elif bm in cache and BOOKMAKER_REGISTRY[bm]["platform"] == "playwright_spa":
                logger.warning(f"[{bm.upper()}-CACHE-EXPIRED] Cached endpoint failed. Re-enabling Playwright Discovery engine.")
                playwright_targets[bm] = BOOKMAKER_REGISTRY[bm]

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
                pw_tasks = [
                    intercept_playwright_spa(browser, bm, cfg)
                    for bm, cfg in playwright_targets.items()
                ]
                pw_results = await asyncio.gather(*pw_tasks, return_exceptions=True)
                for res in pw_results:
                    if isinstance(res, list):
                        all_matches.extend([x for x in res if isinstance(x, dict)])
                await browser.close()
        except Exception as e:
            logger.error(f"Playwright batch error: {repr(e)}")

    arbitrage_ops = find_arbitrage_opportunities(all_matches, bankroll=100000.0)
    logger.info(f"=== SCAN COMPLETED: {len(all_matches)} matches | {len(arbitrage_ops)} arbitrage opportunities ===")

    for idx, arb in enumerate(arbitrage_ops[:5], 1):
        logger.info(
            f"SUREBET #{idx}: +{arb['profit_margin_pct']}% | Event: {arb['event']} "
            f"({arb['competition']}) [{arb['sport']}] | Total Invest: {arb['total_investment']:,} | Payout: {arb['guaranteed_payout']:,}"
        )
        for leg in arb["legs"]:
            logger.info(f"  - Bet {leg['recommended_stake']:,} on {leg['bookmaker']} @ {leg['odds']} for {leg['outcome']}")

    return {
        "matches_count": len(all_matches),
        "arbitrage_opportunities_count": len(arbitrage_ops),
        "arbitrage_opportunities": arbitrage_ops,
        "matches": all_matches
    }


# -------------------------------------------------------------------
# WEB SERVICE BOILERPLATE
# -------------------------------------------------------------------
def build_web_app():
    if not FASTAPI_AVAILABLE:
        raise RuntimeError("FastAPI is not installed. Install fastapi to run in web mode.")
    app = FastAPI()

    @app.get("/health")
    def health():
        return {"status": "ok"}

    @app.get("/scrape")
    async def run_scrape():
        try:
            return await scrape_all_sportsbooks()
        except Exception as e:
            logger.exception(e)
            return {"error": str(e)}

    @app.on_event("startup")
    async def startup_event():
        interval = SCRAPER_INTERVAL
        asyncio.create_task(periodic_scrape(interval))

    async def periodic_scrape(interval: int):
        while True:
            try:
                await scrape_all_sportsbooks()
            except Exception as e:
                logger.exception(e)
            await asyncio.sleep(interval)

    return app


# -------------------------------------------------------------------
# ENTRYPOINT
# -------------------------------------------------------------------
if __name__ == "__main__":
    if SCRAPER_MODE == "web" and FASTAPI_AVAILABLE:
        port = int(os.getenv("PORT", "8000"))
        app = build_web_app()
        if UVICORN_AVAILABLE:
            logger.info(f"Starting web scraper on port {port} (web mode).")
            uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")
        else:
            logger.error("uvicorn is not available; cannot start web server.")
    else:
        logger.info("Starting scraper as background worker (no port binding).")

        async def main_loop():
            while True:
                try:
                    await scrape_all_sportsbooks()
                except Exception as e:
                    logger.exception(e)
                await asyncio.sleep(SCRAPER_INTERVAL)

        asyncio.run(main_loop())
