#!/usr/bin/env python3
"""
Robust Odds Scraper + Arbitrage Engine (amended, all eight improvements)

Improvements implemented (1-8):
1) Deeper WebSocket integration for real-time odds
2) Robust rate limiting with exponential backoff + per-bookmaker proxy support
3) Safer, extensible parser with a stronger fallback
4) Externalized registry via bookmakers.yaml (overrides)
5) Improved event fingerprinting and deduplication (with normalization)
6) Enhanced Playwright waiting logic (selectors, network idle, click-to-load-more)
7) Data normalization (aliases, competition normalization, consistent odds)
8) Safer asyncio.gather usage with per-task error handling

Notes:
- Create bookmakers.yaml to override DEFAULT_BOOKMAKER_REGISTRY.
- This script focuses on clarity and robustness. Adapt the registry to your environment.
"""

import asyncio
import json
import logging
import os
import re
import time
import zlib
import difflib
from collections import Counter
from typing import Any, Dict, List, Optional

from urllib.parse import urlparse

try:
    import yaml  # type: ignore
except Exception:
    yaml = None

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
SCRAPER_PROXY = os.getenv("SCRAPER_PROXY")  # Optional global proxy

if SAVE_DIAGNOSTICS and not os.path.exists(DIAGNOSTICS_DIR):
    os.makedirs(DIAGNOSTICS_DIR, exist_ok=True)

IGNORE_JSON_KEYWORDS = (
    "growthbook", "analytics", "geo-location", "config", "translations",
    "feature", "consent", "cookies", "telemetry", "google-analytics", "facebook"
)

# Stealth browser
try:
    from patchright.async_api import Browser, async_playwright
    logger.info("Using patchright for browser automation (stealth).")
except Exception:
    from playwright.async_api import Browser, async_playwright
    logger.warning("patchright not available; using plain Playwright.")


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

# Sport map
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

# WebSocket odds state
WEB_SOCKET_ODDS_STATE: Dict[str, Dict[str, Dict[str, Optional[float]]]] = {}

# -------------------------------------------------------------------
# ENDPOINT DISCOVERY & REGISTRY
# -------------------------------------------------------------------
def load_endpoint_cache() -> Dict[str, Any]:
    if os.path.exists(CACHE_FILE):
        try:
            with open(CACHE_FILE, "r", encoding="utf-8") as f:
                cache_data = json.load(f)
            # prune TTL
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

def load_external_registry(base_registry: Dict[str, Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """Merge external bookmaker registry overrides from bookmakers.yaml if available."""
    reg = dict(base_registry)
    if yaml is None:
        return reg
    if not os.path.exists("bookmakers.yaml"):
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

# -------------------------------------------------------------------
# UTILITY & NORMALIZATION
# -------------------------------------------------------------------
def normalize_team_name(name: Any) -> str:
    if not name:
        return ""
    s = str(name).strip().lower()
    # basic aliases
    aliases = {
        "man utd": "manchester united",
        "man united": "manchester united",
        "man city": "manchester city",
        "fc barcelona": "barcelona",
        "fc madrid": "real madrid",
    }
    s = aliases.get(s, s)
    return s

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
    if "basket" in val_str:
        return "basketball"
    if "tennis" in val_str and "table" not in val_str:
        return "tennis"
    if "table" in val_str or "ping" in val_str:
        return "table_tennis"
    if "volley" in val_str:
        return "volleyball"
    if any(k in val_str for k in ["mma", "ufc", "fighting", "boxing"]):
        return "mma"
    if "handball" in val_str:
        return "handball"
    if "dart" in val_str:
        return "darts"
    return val_str.replace(" ", "_")

def generate_event_fingerprint(home: str, away: str, sport: str) -> str:
    # Normalized fingerprint to improve cross-bookmaker dedup
    home_n = normalize_team_name(home)
    away_n = normalize_team_name(away)
    if not home_n or not away_n:
        return f"{sport}_{home.lower()[:6]}_{away.lower()[:6]}"
    return f"{sport}_{home_n[:6]}_vs_{away_n[:6]}"

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

# Basic dedup across matches; can enhance with fuzzy matching
def deduplicate_matches(all_matches: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    if not all_matches:
        return []
    seen: Dict[str, Dict[str, Any]] = {}
    for m in all_matches:
        if not isinstance(m, dict):
            continue
        # Build a stable key
        key = f"{m.get('bookmaker_id','')}|{m.get('match_id','')}"
        if key not in seen:
            seen[key] = m
        else:
            # Prefer the one with more complete odds
            existing = seen[key]
            if (m.get("home_odds") is not None) and (existing.get("home_odds") is None):
                seen[key] = m
            elif (m.get("away_odds") is not None) and (existing.get("away_odds") is None):
                seen[key] = m
    return list(seen.values())

# SSR hydration finder
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

# Recursive extractor for unknown payloads
def find_events_recursive(obj: Any, depth: int = 0) -> List[Dict[str, Any]]:
    if depth > 4:
        return []
    if isinstance(obj, list):
        if obj and isinstance(obj[0], dict):
            return [x for x in obj if isinstance(x, dict)]
        events = []
        for item in obj[:10]:
            if isinstance(item, (dict, list)):
                events.extend(find_events_recursive(item, depth + 1))
        return events
    if isinstance(obj, dict):
        for k in ["events", "matches", "games", "fixtures", "items", "groupList", "matchVoList", "data", "sportsTree", "results"]:
            if k in obj:
                v = obj[k]
                if isinstance(v, list):
                    return [x for x in v if isinstance(x, dict)]
                if isinstance(v, dict):
                    found = find_events_recursive(v, depth + 1)
                    if found:
                        return found
        # generic descent
        for v in obj.values():
            if isinstance(v, (dict, list)):
                found = find_events_recursive(v, depth + 1)
                if found:
                    return found
    return []

# Cross-bookmaker arbitrage
def find_arbitrage_opportunities(all_matches: List[Dict[str, Any]], bankroll: float = 100000.0) -> List[Dict[str, Any]]:
    grouped: Dict[str, List[Dict[str, Any]]] = {}
    for m in all_matches:
        if not isinstance(m, dict):
            continue
        home = str(m.get("home_team", "")).strip()
        away = str(m.get("away_team", "")).strip()
        sport = str(m.get("sport", "soccer"))
        if not home or not away:
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

        if sport in ["tennis","basketball","volleyball","mma","table_tennis","baseball","darts","handball"]:
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
                        {"outcome": f"1 ({home_name})", "bookmaker": str(best_home.get("bookmaker_id","")).upper(),
                         "odds": o1, "recommended_stake": stake1, "expected_payout": payout1},
                        {"outcome": f"2 ({away_name})", "bookmaker": str(best_away.get("bookmaker_id","")).upper(),
                         "odds": o2, "recommended_stake": stake2, "expected_payout": payout2},
                    ],
                })
        else:
            best_draw = max([m for m in valid_matches if m.get("draw_odds") is not None],
                            key=lambda x: x.get("draw_odds") or 0, default=None)
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
                        {"outcome": f"1 ({home_name})", "bookmaker": str(best_home.get("bookmaker_id","")).upper(),
                         "odds": o1, "recommended_stake": stake1, "expected_payout": payout1},
                        {"outcome": "X (Draw)", "bookmaker": str(best_draw.get("bookmaker_id","")).upper(),
                         "odds": oX, "recommended_stake": stakeX, "expected_payout": payoutX},
                        {"outcome": f"2 ({away_name})", "bookmaker": str(best_away.get("bookmaker_id","")).upper(),
                         "odds": o2, "recommended_stake": stake2, "expected_payout": payout2},
                    ],
                })

    # Lightweight dedup across opportunities
    deduped: Dict[str, Dict[str, Any]] = {}
    for o in opportunities:
        key = f"{o['sport']}|{o['competition']}|{o['event']}"
        if key not in deduped or o.get("profit_margin_pct", 0) > deduped[key].get("profit_margin_pct", 0):
            deduped[key] = o
    return list(deduped.values())

# -------------------------------------------------------------------
# BOOKMAKER REGISTRY
# -------------------------------------------------------------------
DEFAULT_BOOKMAKER_REGISTRY: Dict[str, Dict[str, Any]] = {
    "betika": {"platform": "public_rest", "url": "https://api.betika.com/v1/uo/matches?limit=100&sub_type=prematch", "parser": "betika", "timeout": 8},
    "sportybet": {"platform": "public_rest", "url": "https://www.sportybet.com/api/tz/factsCenter/pcUpcomingEvents?pageSize=100&pageNum=1&option=1", "parser": "sportybet", "timeout": 8},
    "bangbet": {"platform": "public_rest", "url": "https://bet-api.bangbet.com/api/bet/match/listTop?country=tz", "parser": "bangbet", "timeout": 8},
    "leonbet": {"platform": "public_rest", "url": "https://leonbet.co.tz/api-2/betline/events/all?ctag=en-US", "parser": "leonbet", "timeout": 25},
    "premierbet": {"platform": "public_rest", "url": "https://sports-api.premierbet.co.tz/v1/events/highlights?country=TZ&group=g2&platform=desktop&locale=sw&limit=100", "parser": "premierbet", "timeout": 8},
    "meridianbet": {"platform": "public_rest", "url": "https://online.meridianbet.co.tz/api/v2/events/standard", "parser": "meridianbet", "timeout": 10},
    # 1xCorp-like clones
    "22bet": {"platform": "public_rest", "url": "https://22bet.co.tz/service-api/LiveFeed/Get1x2_VZip?count=50&lng=en_GB&gr=329&mode=4&country=181&partner=151", "parser": "1xcorp", "timeout": 10},
    "helabet": {"platform": "public_rest", "url": "https://helabet.co.tz/service-api/LiveFeed/Get1x2_VZip?count=50&lng=en&gr=329&mode=4&country=181&partner=237", "parser": "1xcorp", "timeout": 10},
    "betwinner": {"platform": "public_rest", "url": "https://betwinner.co.tz/service-api/LiveFeed/Get1x2_VZip?count=50&lng=en&gr=329&mode
