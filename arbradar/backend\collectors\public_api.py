"""Collectors for public, unauthenticated sportsbook market APIs."""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from playwright.sync_api import sync_playwright

from backend.collector import save_odds
from backend.collectors.metadata import bookmaker_family
from backend.utils.playwright_browser import launch_sync_browser

PROJECT_ROOT = Path(__file__).resolve().parents[2]
COLLECTOR_HEALTH = PROJECT_ROOT / "reports" / "collector-health.json"

SIMULATED_EVENT_TERMS = (
    "virtual", "simulated", "simulation", "simulator", "e-soccer", "esoccer",
    "e-football", "efootball", "e-basketball", "ebasketball", "e-tennis", "etennis",
    "virtual sports", "cyber football",
)


def is_real_sports_event(event: dict) -> bool:
    """Reject virtual/simulated fixtures before odds are saved or compared."""
    searchable = " ".join((
        str(event.get("name", "")), str(event.get("sport", "")),
        str(event.get("league", "")), str(event.get("competition", "")),
    )).casefold()
    return not any(term in searchable for term in SIMULATED_EVENT_TERMS)


def get_json(url: str, timeout: int = 20, headers: dict | None = None) -> dict:
    request = Request(url, headers={"Accept": "application/json", "User-Agent": "ArbRadar/1.0", **(headers or {})})
    for attempt in range(3):
        try:
            with urlopen(request, timeout=timeout) as response:
                return json.load(response)
        except HTTPError as error:
            if error.code not in {500, 502, 503, 504} or attempt == 2:
                raise
            time.sleep(0.5 * (attempt + 1))


def payload(events: list[dict]) -> dict:
    events = [event for event in events if is_real_sports_event(event)]
    changes = [
        {"match": f"{event['name']} — {selection['name']} ({event['site']})", "old": None, "new": selection["odds"]}
        for event in events for selection in (event.get("markets") or [{}])[0].get("selections", [])
    ]
    for event in events:
        event.pop("site", None)
    return {"events": events, "arbs": [], "oddsChanges": changes, "marginHistory": [], "collectedAt": datetime.now(timezone.utc).isoformat()}


def persist_collector_health(health: dict) -> None:
    COLLECTOR_HEALTH.parent.mkdir(parents=True, exist_ok=True)
    temporary = COLLECTOR_HEALTH.with_name(f"collector-health.{os.getpid()}.{time.time_ns()}.tmp")
    temporary.write_text(json.dumps(health, indent=2), encoding="utf-8")
    temporary.replace(COLLECTOR_HEALTH)


def read_collector_health() -> dict:
    try:
        return json.loads(COLLECTOR_HEALTH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"updatedAt": None, "bookmakers": {}}


def total_market_name(raw_name: object, line: object, sport: object = "") -> str:
    """Retain a feed's total category; infer the normal total stat by sport if omitted."""
    label = str(raw_name or "").lower()
    category = next((name for token, name in (
        ("corner", "Corners"), ("goal", "Goals"), ("point", "Points"),
        ("run", "Runs"), ("game", "Games"), ("set", "Sets"),
        ("card", "Cards"), ("shot", "Shots"), ("ace", "Aces"),
    ) if token in label), None)
    sport_name = str(sport or "").lower()
    if category is None and "score" in label and not any(x in sport_name for x in ("football", "soccer", "handball", "hockey")):
        category = "Points"
    if category is None:
        category = next((name for token, name in (
            ("football", "Goals"), ("soccer", "Goals"), ("handball", "Goals"), ("hockey", "Goals"),
            ("basketball", "Points"), ("baseball", "Runs"), ("tennis", "Games"),
            ("table tennis", "Points"), ("volleyball", "Points"), ("rugby", "Points"),
        ) if token in sport_name), "Unspecified Total")
    return f"Total {category} Over/Under {str(line or '').strip()}".strip()


def collect_betika() -> dict:
    """Collect primary markets for all sports exposed by Betika's public feed."""
    sport_ids = (1, 2, 3, 4, 5, 6, 7)  # basketball, hockey, tennis, handball, soccer, table tennis, volleyball
    events = []
    for sport_id in sport_ids:
        url = (
            "https://api.betika.co.tz/v1/uo/matches?"
            f"sport_id={sport_id}&page=1&limit=100&keyword=&tab=&sub_type_id=1,186&tag_id=&country_id=1&language=en"
        )
        for item in get_json(url).get("data", []):
            home, away = item.get("home_team"), item.get("away_team")
            if not home or not away:
                continue
            markets = []
            for market in item.get("odds") or []:
                raw_name = str(market.get("name", "")).strip()
                key = raw_name.lower().replace("-", " ").replace("/", " ")
                if key not in {"1x2", "winner", "moneyline", "12", "over under", "total", "handicap", "asian handicap", "both teams to score", "btts", "odd even"}:
                    continue
                selections = []
                for odd in market.get("odds") or []:
                    name = odd.get("display_name") or odd.get("display")
                    price = odd.get("odd_value")
                    if name and price not in (None, ""):
                        selections.append({"name": name, "odds": price})
                line = str(market.get("special_bet_value") or "").strip()
                canonical = "1x2" if key == "1x2" else raw_name
                if line and canonical.lower() not in {"1x2", "winner", "moneyline", "12"}:
                    canonical = f"{canonical} {line}"
                expected = 3 if canonical.lower() == "1x2" else 2
                if len(selections) == expected:
                    markets.append({"name": canonical, "selections": selections})
            if markets:
                events.append({"name": f"{home} vs {away}", "sport": item.get("sport_name", ""), "site": "betika", "markets": markets})
    result = payload(events)
    save_odds("betika", result)
    return result


def collect_leonbet() -> dict:
    # These parameters match the current public feed used by LeonBet's own
    # sportsbook page. The older mm2/rrc request now returns HTTP 500.
    url = "https://leonbet.co.tz/api-2/betline/headline-matches?ctag=en-US-7da6p&flags=reg,urlv2,orn2,cn,mm3,ssn,rrc,cmg&merged=true"
    events = []
    for item in get_json(url).get("events", {}).get("events", []):
        competitors = {competitor.get("homeAway"): competitor.get("name") for competitor in item.get("competitors", [])}
        if not competitors.get("HOME") or not competitors.get("AWAY"):
            continue
        markets = []
        sport = (item.get("league") or {}).get("sport", {}).get("name", "")
        for market in item.get("markets", []):
            raw_name = str(market.get("name", "")).strip()
            runners = [runner for runner in market.get("runners", []) if runner.get("open", True)]
            tags = {tag: runner for runner in runners for tag in runner.get("tags", [])}
            if raw_name.lower() == "winner" and market.get("cols") in {2, 3}:
                selections = [
                    {"name": competitors["HOME"], "odds": (tags.get("HOME") or {}).get("price")},
                    {"name": competitors["AWAY"], "odds": (tags.get("AWAY") or {}).get("price")},
                ]
                if market.get("cols") == 3:
                    selections.insert(1, {"name": "draw", "odds": (tags.get("DRAW") or {}).get("price")})
                if all(selection["odds"] is not None for selection in selections):
                    markets.append({"name": "1x2" if len(selections) == 3 else "winner", "selections": selections})
            elif raw_name.lower() == "total":
                by_side = {tag: runner for tag, runner in tags.items() if tag in {"OVER", "UNDER"}}
                if set(by_side) != {"OVER", "UNDER"}:
                    continue
                over, under = by_side["OVER"], by_side["UNDER"]
                line = over.get("handicap") or under.get("handicap") or (market.get("specifiers") or {}).get("total")
                if line is None or str(line).strip() != str(under.get("handicap", line)).strip():
                    continue
                markets.append({
                    "name": total_market_name(raw_name, line, sport),
                    "selections": [
                        {"name": f"Over {line}", "odds": over.get("price")},
                        {"name": f"Under {line}", "odds": under.get("price")},
                    ],
                })
        if markets:
            events.append({"name": f"{competitors['HOME']} vs {competitors['AWAY']}", "sport": sport, "site": "leonbet", "markets": markets})
    result = payload(events)
    save_odds("leonbet", result)
    return result


def collect_premierbet() -> dict:
    """Read Premier Bet's public sports API in a normal browser context.

    The API responds with 403 to a bare HTTP client, but is publicly requested
    by the sportsbook page itself. No login, proxy, or access-control bypass is
    used here.
    """
    date = datetime.now(timezone.utc).date().isoformat()
    cms_sports = "https://cms-ui-data-prd.sahara.editec-online.com/TZ/sports/desktop/sw"
    with sync_playwright() as playwright:
        browser = launch_sync_browser(playwright)
        page = browser.new_page(viewport={"width": 1440, "height": 900}, locale="sw-TZ")
        page.goto("https://premierbet.co.tz", wait_until="domcontentloaded", timeout=30_000)
        feeds = page.evaluate(
            """async ({cmsUrl, date}) => {
                const sports = await fetch(cmsUrl).then(r => r.json());
                const supported = [...new Map(sports
                    .filter(s => /^\\d+$/.test(String(s.providerId || '')) && s.targetUri?.startsWith('/sport/'))
                    .filter(s => !/zoom|virtual|e-soccer|esoccer|odds-boost/i.test(`${s.name} ${s.targetUri}`))
                    .map(s => [String(s.providerId), {id: String(s.providerId), name: s.name}])).values()];
                const output = [];
                for (const sport of supported) {
                    try {
                        const url = `https://sports-api.premierbet.co.tz/v1/events/upcoming?country=TZ&group=g2&platform=desktop&locale=sw&timeOffset=-180&sportId=${encodeURIComponent(sport.id)}&pageId=6437eeeb374df46eec443294&date=${date}`;
                        const data = await fetch(url).then(r => r.ok ? r.json() : null);
                        if (data) output.push({sport: sport.name, data});
                    } catch (_) {}
                }
                return output;
            }""",
            {"cmsUrl": cms_sports, "date": date},
        )
        browser.close()

    events = []
    for feed in feeds:
        data = feed.get("data", {})
        for category in data.get("data", {}).get("categories", []):
            for competition in category.get("competitions", []):
                for event in competition.get("events", []):
                    names = event.get("eventNames", [])
                    if len(names) != 2:
                        continue
                    markets = []
                    for market in event.get("markets", []):
                        raw_name = str(market.get("name", "")).strip()
                        key = raw_name.lower().replace("/", " ").replace("-", " ")
                        if key not in {"1x2", "winner", "moneyline", "12", "over under", "handicap", "asian handicap", "both teams to score", "btts", "odd even"}:
                            continue
                        outcome_map = {}
                        for outcome in market.get("outcomes", []):
                            if not outcome.get("active"):
                                continue
                            code = str(outcome.get("name", "")).strip().lower()
                            label = {"1": names[0], "home": names[0], "x": "draw", "draw": "draw", "2": names[1], "away": names[1]}.get(code, outcome.get("name"))
                            if label:
                                outcome_map[str(label).lower()] = {"name": label, "odds": outcome.get("value")}
                        if key == "1x2":
                            selections = [outcome_map.get(name.lower()) for name in (names[0], "draw", names[1])]
                        elif key in {"winner", "moneyline", "12"}:
                            selections = [outcome_map.get(names[0].lower()), outcome_map.get(names[1].lower())]
                        else:
                            selections = [value for value in outcome_map.values()]
                        selections = [selection for selection in selections if selection and selection["odds"] is not None]
                        line = str(market.get("specifier") or market.get("line") or "").strip()
                        market_name = "1x2" if key == "1x2" else raw_name
                        if line and key not in {"1x2", "winner", "moneyline", "12"}:
                            market_name = f"{market_name} {line}"
                        if len(selections) in {2, 3}:
                            markets.append({"name": market_name, "selections": selections})
                    if markets:
                        events.append({"name": f"{names[0]} vs {names[1]}", "sport": feed.get("sport", ""), "site": "premierbet", "markets": markets})
    result = payload(events)
    save_odds("premierbet", result)
    return result


def collect_wasafibet() -> dict:
    """Read the public football market feed requested by WasafiBet's own page."""
    endpoint = "https://api.wasafibet.com/wb/sportsbook?sport_id=soccer&src=1&producer=3&type=matches&profile_id=&msisdn=&bm_leagues=&resource=sport&attribution=&platform=desktop"
    captured = []

    with sync_playwright() as playwright:
        browser = launch_sync_browser(playwright)
        page = browser.new_page(locale="en-TZ")

        def capture_response(response):
            if "api.wasafibet.com/wb/sportsbook" not in response.url or "resource=sport&" not in response.url:
                return
            if response.status != 200:
                return
            try:
                captured.append(response.json())
            except Exception:
                return

        page.on("response", capture_response)
        page.goto("https://wasafibet.co.tz", wait_until="domcontentloaded", timeout=45_000)
        page.wait_for_timeout(10_000)
        if not captured:
            page.reload(wait_until="domcontentloaded", timeout=45_000)
            page.wait_for_timeout(10_000)
        browser.close()

    events = []
    for response_data in captured:
        leagues = response_data.get("data", {}).get("leagues", [])
        for league in leagues:
            for match in league.get("matches", []):
                home, away = match.get("home_team"), match.get("away_team")
                if not home or not away:
                    continue
                for market in match.get("markets", []):
                    if str(market.get("odd_type", "")).strip().lower() not in {"1x2", "match result"}:
                        continue
                    line = next(iter(market.get("lines", [])), {})
                    outcomes = line.get("outcomes", [])
                    selections = []
                    for outcome in outcomes:
                        if not outcome.get("active", 1) or not outcome.get("status", 1):
                            continue
                        name = outcome.get("outcome_name")
                        if not name:
                            key = str(outcome.get("outcome_key", "")).upper()
                            name = {"1": home, "X": "draw", "2": away}.get(key)
                        try:
                            odds = float(outcome.get("odd_value"))
                        except (TypeError, ValueError):
                            continue
                        if name and 1.01 <= odds <= 1_000:
                            selections.append({"name": name, "odds": odds})
                    if len(selections) == 3 and len({item["name"].lower() for item in selections}) == 3:
                        events.append({"name": f"{home} vs {away}", "sport": "Football", "site": "wasafibet", "markets": [{"name": "1x2", "selections": selections}]})
                    break

    result = payload(events)
    if events:
        save_odds("wasafibet", result)
    else:
        print("WasafiBet returned no complete public 1X2 markets this cycle; previous report retained.")
    return result


def collect_bangbet() -> dict:
    """Collect real-sport fixture markets from BangBet's public sportsbook API."""
    api = "https://bet-api.bangbet.com/api/bet"
    events_by_id: dict[str, dict] = {}

    with sync_playwright() as playwright:
        browser = launch_sync_browser(playwright)
        page = browser.new_page(locale="en-TZ")
        # This page can leave optional scripts open indefinitely before
        # DOMContentLoaded. The public API calls below only need a committed
        # first-party origin for browser fetch/CORS, so don't wait for every
        # page resource to finish loading.
        page.goto("https://www.bangbet.com/tz-m/", wait_until="commit", timeout=25_000)

        sport_response = page.evaluate(
            "url => fetch(url).then(response => response.json())",
            f"{api}/sport/producer/list?country=tz",
        )
        sports = [
            sport for sport in sport_response.get("data", [])
            if sport.get("producer") == 3
            and sport.get("sportId")
            and not str(sport.get("name", "")).lower().startswith(("v-", "virtual"))
        ]

        async_payload = """async ({url, body}) => {
            const response = await fetch(url, {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify(body)
            });
            if (!response.ok) return {httpStatus: response.status};
            return response.json();
        }"""

        for sport in sports:
            sport_id = sport["sportId"]
            base_body = {
                "sportId": sport_id, "groupIndex": 0, "tournamentId": "", "producer": 3,
                "position": 17, "beginTime": "", "highLight": True, "endTime": "",
                "showMarket": True, "timeZone": "+3", "page": 1, "sortType": 1,
                "pageSize": 100, "marketChildrenIndex": 0, "pageNo": 1,
                "minOdds": None, "maxOdds": None, "isUpMarket": True, "dataGroup": False,
            }

            try:
                first_response = page.evaluate(async_payload, {"url": f"{api}/match/list?country=tz", "body": base_body})
                data = first_response.get("data") or {}
                menus = data.get("marketGroupMenus") or []
                selected_groups = []
                for index, menu in enumerate(menus):
                    name = str(menu.get("name", "")).lower()
                    if index == 0 or any(
                        word in name for word in ("over/under", "total", "handicap", "spread", "gg/ng", "both teams", "odd/even", "odd even")
                    ):
                        selected_groups.append((index, name))

                group_responses = [(0, first_response)]
                for group_index, _ in selected_groups:
                    if group_index == 0:
                        continue
                    request_body = {**base_body, "groupIndex": group_index}
                    response_data = page.evaluate(async_payload, {"url": f"{api}/match/list?country=tz", "body": request_body})
                    group_responses.append((group_index, response_data))

                for group_index, response_data in group_responses:
                    result_data = response_data.get("data") or {}
                    group_name = str((menus[group_index] if group_index < len(menus) else {}).get("name", ""))
                    for match in result_data.get("data") or []:
                        home = match.get("homeTeamName")
                        away = match.get("awayTeamName")
                        event_id = match.get("id")
                        if not home or not away or not event_id or not match.get("active", True):
                            continue
                        event = events_by_id.setdefault(
                            event_id,
                            {"name": f"{home} vs {away}", "sport": sport.get("name", ""), "site": "bangbet", "markets": []},
                        )
                        for market_group in match.get("marketList") or []:
                            for market in market_group.get("markets") or []:
                                outcomes = [outcome for outcome in market.get("outcomes") or [] if outcome.get("active") and outcome.get("odds")]
                                if len(outcomes) not in {2, 3}:
                                    continue
                                market_label = str(market.get("name") or group_name).strip()
                                label_key = market_label.lower()
                                if "1x2" in label_key:
                                    labels = {"1": home, "x": "draw", "2": away, "home": home, "away": away}
                                    selections = [{"name": labels.get(str(outcome.get("desc", "")).strip().lower(), outcome.get("desc")), "odds": outcome.get("odds")} for outcome in outcomes]
                                    canonical = "1x2" if len(selections) == 3 and any(str(item["name"]).lower() == "draw" for item in selections) else "winner"
                                elif "over" in label_key or "under" in label_key or "total" in label_key:
                                    specifiers = str(market.get("specifiers") or "")
                                    line_match = re.search(r"(?:total=)?(\d+(?:\.\d+)?)", specifiers)
                                    if not line_match:
                                        line_match = re.search(r"\d+(?:\.\d+)?", " ".join(str(outcome.get("desc", "")) for outcome in outcomes))
                                    # A total without its numeric line cannot be
                                    # matched safely with another bookmaker.
                                    if not line_match:
                                        continue
                                    line = line_match.group(1) if line_match and line_match.lastindex else (line_match.group(0) if line_match else "")
                                    canonical = total_market_name(market_label, line, sport.get("name", ""))
                                    selections = [{"name": "over" if str(outcome.get("desc", "")).strip().lower().startswith("over") else "under", "odds": outcome.get("odds")} for outcome in outcomes]
                                elif "gg/ng" in label_key or "both teams to score" in label_key or "btts" in label_key:
                                    canonical = "Both Teams to Score"
                                    selections = [{"name": "yes" if str(outcome.get("desc", "")).strip().lower() in {"gg", "yes", "y"} else "no", "odds": outcome.get("odds")} for outcome in outcomes]
                                elif "odd/even" in label_key or "odd even" in label_key or "even/odd" in label_key:
                                    canonical = "Odd/Even"
                                    selections = [{"name": "odd" if str(outcome.get("desc", "")).strip().lower().startswith("odd") else "even", "odds": outcome.get("odds")} for outcome in outcomes]
                                elif "handicap" in label_key or "spread" in label_key:
                                    specifiers = str(market.get("specifiers") or "")
                                    line_match = re.search(r"[-+]?(?:\d+\.?\d*)", specifiers)
                                    line = line_match.group(0) if line_match else ""
                                    canonical = f"Handicap {line}".strip()
                                    labels = {"home": home, "away": away, "1": home, "2": away}
                                    selections = [{"name": labels.get(str(outcome.get("desc", "")).strip().lower(), outcome.get("desc")), "odds": outcome.get("odds")} for outcome in outcomes]
                                else:
                                    continue
                                if len(selections) in {2, 3}:
                                    event["markets"].append({"name": canonical, "selections": selections})
            except Exception as error:
                print(f"BangBet {sport.get('name', sport_id)} feed skipped this cycle: {type(error).__name__}: {error}")
        browser.close()

    events = list(events_by_id.values())
    events = [event for event in events if event["markets"]]
    result = payload(events)
    if events:
        save_odds("bangbet", result)
    return result


def collect_thronebet() -> dict:
    """Collect public real-sport markets from ThroneBet's sportsbook graph API."""
    endpoint = "https://api.thronebet.com/api/v2/multi"
    event_rows: dict[str, dict] = {}
    sport_query = """mutation { sportList(sportListInput: {rangeFilter: ""}) {
        sportId sportName visible eventsCount
    } }"""
    events_query = """mutation { mainEventList(mainEventListInput: {
        topEvents: false page: 1 sportId: __SPORT_ID__ limit: 40
    }) { sportId sportName competitions { competitionId competitionName events {
        eventId eventName eventStartTime collections { collectionId collectionName markets {
            marketId marketName marketCode prices { priceName handicapValue rate createdDate }
        } }
    } } } }"""

    with sync_playwright() as playwright:
        browser = launch_sync_browser(playwright)
        page = browser.new_page(locale="en-TZ")
        page.goto("https://thronebet.com/", wait_until="domcontentloaded", timeout=35_000)

        def make_query(query: str):
            body = [{"module": "graphs", "method": "makeQuery", "options": {"query": query}}]
            response = page.evaluate(
                "async body => { const r = await fetch('https://api.thronebet.com/api/v2/multi', "
                "{method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)}); "
                "if (!r.ok) return {httpStatus:r.status}; return r.json(); }",
                body,
            )
            if isinstance(response, list) and response:
                return response[0].get("data", {})
            return {}

        sport_data = make_query(sport_query)
        for sport in sport_data.get("sportList", []):
            sport_name = str(sport.get("sportName", "")).strip()
            if not sport.get("visible", True) or not sport.get("sportId") or any(
                term in sport_name.lower() for term in ("virtual", "casino", "esoccer", "virtual football")
            ):
                continue
            query = events_query.replace("__SPORT_ID__", str(sport["sportId"]))
            for listed_sport in make_query(query).get("mainEventList", []):
                for competition in listed_sport.get("competitions", []):
                    for item in competition.get("events", []):
                        event_name = str(item.get("eventName", ""))
                        parts = re.split(r"\s+v(?:s\.?)?\s+", event_name, maxsplit=1, flags=re.IGNORECASE)
                        if len(parts) != 2:
                            continue
                        home, away = (part.strip() for part in parts)
                        event_id = str(item.get("eventId") or "")
                        if not event_id:
                            continue
                        markets = []
                        for collection in item.get("collections", []):
                            for market in collection.get("markets", []):
                                code = str(market.get("marketCode", "")).lower()
                                market_label = str(market.get("marketName", "")).strip()
                                prices = [price for price in market.get("prices", []) if not price.get("blocked") and float(price.get("rate") or 0) > 1]
                                if code == "1x2" and len(prices) in {2, 3}:
                                    labels = {"1": home, "x": "draw", "2": away, "home": home, "away": away}
                                    selections = [{"name": labels.get(str(price.get("priceName", "")).strip().lower(), price.get("priceName")), "odds": price.get("rate")} for price in prices]
                                    canonical = "1x2" if len(selections) == 3 and any(str(selection["name"]).lower() == "draw" for selection in selections) else "winner"
                                elif code in {"bts", "btts"} and len(prices) == 2:
                                    canonical = "Both Teams to Score"
                                    selections = [{"name": "yes" if str(price.get("priceName", "")).strip().lower() in {"yes", "y", "gg"} else "no", "odds": price.get("rate")} for price in prices]
                                elif code in {"odd_even", "oddeven", "odd/even"} and len(prices) == 2:
                                    canonical = "Odd/Even"
                                    selections = [{"name": "odd" if str(price.get("priceName", "")).strip().lower().startswith("odd") else "even", "odds": price.get("rate")} for price in prices]
                                elif "over" in market_label.lower() and "under" in market_label.lower() and len(prices) == 2:
                                    line = next((price.get("handicapValue") for price in prices if price.get("handicapValue") not in (None, 0)), "")
                                    canonical = total_market_name(market_label, line, sport_name)
                                    selections = [{"name": "over" if str(price.get("priceName", "")).strip().lower().startswith("over") else "under", "odds": price.get("rate")} for price in prices]
                                else:
                                    continue
                                markets.append({"name": canonical, "selections": selections})
                        if markets:
                            event_rows[event_id] = {
                                "name": f"{home} vs {away}", "sport": sport_name,
                                "site": "thronebet", "markets": markets,
                            }
        browser.close()

    events = list(event_rows.values())
    result = payload(events)
    if events:
        save_odds("thronebet", result)
    return result


def collect_linefeed(site: str, domain: str) -> dict:
    """Collect public real-sport pre-match odds from a 1xBet-family line feed."""
    sports = {
        1: "Football", 4: "Tennis", 3: "Basketball", 2: "Ice Hockey",
        6: "Volleyball", 10: "Table Tennis", 16: "Badminton", 21: "Darts",
        66: "Cricket", 5: "Baseball", 8: "Handball", 13: "American Football",
        14: "Futsal", 30: "Snooker",
    }
    base = f"https://{domain}/service-api/LineFeed/Get1x2_VZip"
    params = "count=100&lng=en_GB&tz=3&mode=4&country=181&partner=151&getEmpty=true&gr=329"
    events_by_id: dict[str, dict] = {}

    def fetch_sport(entry: tuple[int, str]) -> dict:
        sport_id, sport_name = entry
        url = f"{base}?sports={sport_id}&{params}"
        for attempt in range(3):
            try:
                data = get_json(url, timeout=60 if site == "megapari" else 20)
                return {"id": sport_id, "name": sport_name, "value": data.get("Value", [])}
            except Exception as error:
                if attempt == 2:
                    return {"id": sport_id, "name": sport_name, "error": f"{type(error).__name__}: {error}", "value": []}
                time.sleep(0.4 * (attempt + 1))
        return {"id": sport_id, "name": sport_name, "value": []}

    # Fetch feeds directly to avoid site-page navigation/CORS failures on clone skins.
    with ThreadPoolExecutor(max_workers=3) as executor:
        batches = list(executor.map(fetch_sport, sports.items()))

        for batch in batches:
            if batch.get("error") or batch.get("httpStatus"):
                detail = batch.get("error") or f"HTTP {batch['httpStatus']}"
                print(f"{site} {batch.get('name', batch.get('id'))} feed failed this cycle: {detail}")
            for item in batch.get("value", []):
                home, away = item.get("O1E") or item.get("O1"), item.get("O2E") or item.get("O2")
                if not home or not away or not item.get("I"):
                    continue
                placeholder_names = {
                    "home", "away", "team 1", "team 2", "participant 1", "participant 2",
                    "home (special bets)", "away (special bets)",
                }
                if home.strip().casefold() in placeholder_names or away.strip().casefold() in placeholder_names:
                    continue
                try:
                    if float(item.get("S") or 0) <= time.time():
                        continue
                except (TypeError, ValueError):
                    pass

                selections_by_market: dict[tuple[str, str], dict[str, dict]] = {}

                def add_outcome(outcome: dict):
                    try:
                        price = float(outcome.get("C") or 0)
                        code = int(outcome.get("T"))
                        group = int(outcome.get("G"))
                    except (TypeError, ValueError):
                        return
                    if price <= 1:
                        return
                    line = outcome.get("P")
                    if group == 1 and code in {1, 2, 3}:
                        key = ("1x2", "")
                        label = {1: home, 2: "draw", 3: away}[code]
                    elif group == 17 and code in {9, 10} and line is not None:
                        normalized_line = f"{float(line):g}"
                        key = ("totals", normalized_line)
                        label = "over" if code == 9 else "under"
                    elif group == 2 and code in {7, 8} and line is not None:
                        signed_line = float(line)
                        normalized_line = f"{abs(signed_line):g}"
                        key = ("handicap", normalized_line)
                        participant = home if code == 7 else away
                        label = f"{participant} ({signed_line:+g})"
                    else:
                        return
                    current = selections_by_market.setdefault(key, {}).get(label)
                    if not current or price > current["odds"]:
                        selections_by_market[key][label] = {"name": label, "odds": price}

                for outcome in item.get("E", []):
                    add_outcome(outcome)
                for alternative in item.get("AE", []):
                    for outcome in alternative.get("ME", []):
                        add_outcome(outcome)

                markets = []
                for (kind, line), selections in selections_by_market.items():
                    if kind == "1x2":
                        expected = {home, "draw", away}
                        if set(selections) == expected:
                            markets.append({"name": "1x2", "selections": list(selections.values())})
                        elif set(selections) == {home, away}:
                            markets.append({"name": "winner", "selections": list(selections.values())})
                    elif kind == "totals" and set(selections) == {"over", "under"}:
                        markets.append({"name": f"Over/Under {line}", "selections": list(selections.values())})
                    elif kind == "handicap" and len(selections) == 2:
                        markets.append({"name": f"Handicap {line}", "selections": list(selections.values())})

                if markets:
                    event_id = str(item["I"])
                    events_by_id[event_id] = {
                        "name": f"{home} vs {away}",
                        "sport": item.get("SE") or item.get("SN") or sports.get(batch.get("id"), ""),
                        "site": site,
                        "markets": markets,
                    }

    events = list(events_by_id.values())
    result = payload(events)
    if events:
        save_odds(site, result)
    return result


def collect_22bet() -> dict:
    return collect_linefeed("22bet", "22bet.co.tz")


def collect_1xbet() -> dict:
    return collect_linefeed("1xbet", "1xbet.tz")


def collect_helabet() -> dict:
    return collect_linefeed("helabet", "helabet.co.tz")


def collect_betwinner() -> dict:
    return collect_linefeed("betwinner", "betwinner.co.tz")


def collect_megapari() -> dict:
    return collect_linefeed("megapari", "megapari.com")


def collect_1xbit() -> dict:
    return collect_linefeed("1xbit", "1xbit.com")


def collect_melbet() -> dict:
    return collect_linefeed("melbet", "melbet.mobi")


def collect_sportpesa() -> dict:
    """SportPesa has a paginated JSON prematch feed and a separate market-details API."""
    base = "https://www.sportpesa.co.tz"
    headers = {
        "Accept": "application/json, text/plain, */*",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36",
        "Referer": f"{base}/sw/nyumbani-betting/soka-1/michezo-inayofuata/",
    }
    sports = {1: "Football", 2: "Basketball", 5: "Tennis", 20: "Table Tennis", 23: "Volleyball",
              6: "Handball", 12: "Rugby Union", 21: "Cricket", 4: "Ice Hockey", 3: "Baseball", 10: "Boxing", 16: "American Football"}
    now = datetime.now(timezone.utc)
    horizon = now + timedelta(days=7)
    events_by_id = {}
    for sport_id, sport_name in sports.items():
        offset = 1
        while offset <= 3000:
            query = urlencode({"type": "prematch", "sportId": sport_id, "section": "upcoming", "markets_layout": "multiple", "o": "leagues", "pag_count": 100, "pag_min": offset})
            rows = get_json(f"{base}/api/upcoming/games?{query}", timeout=30, headers=headers)
            if not isinstance(rows, list) or not rows:
                break
            stop_page = True
            for item in rows:
                try:
                    start = datetime.fromtimestamp(float(item.get("dateTimestamp")) / 1000, timezone.utc)
                except (TypeError, ValueError, OSError):
                    try:
                        start = datetime.fromisoformat(item["date"].replace("Z", "+00:00"))
                    except (KeyError, TypeError, ValueError):
                        continue
                if start < now:
                    continue
                if start > horizon:
                    continue
                stop_page = False
                competitors = item.get("competitors") or []
                if len(competitors) < 2:
                    continue
                home, away = competitors[0].get("name"), competitors[1].get("name")
                markets = []
                for market in item.get("markets") or []:
                    selections = market.get("selections") or []
                    if market.get("name", "").lower() in {"1x2", "match winner", "winner"} and len(selections) in (2, 3):
                        canonical = "1x2" if len(selections) == 3 else "winner"
                        markets.append({"name": canonical, "selections": [{"name": s.get("name"), "odds": s.get("odds")} for s in selections]})
                events_by_id[str(item.get("id"))] = {"name": f"{home} vs {away}", "sport": sport_name, "site": "sportpesa", "markets": markets, "startTime": start.isoformat()}
            if stop_page or len(rows) < 100:
                break
            offset += len(rows)

    # Football's details endpoint supplies two-way, yes/no, odd/even and totals markets.
    football_ids = [event_id for event_id, event in events_by_id.items() if event["sport"] == "Football"]
    for index in range(0, len(football_ids), 20):
        ids = football_ids[index:index + 20]
        if not ids:
            continue
        query = urlencode({"games": ",".join(ids), "markets": "43,45,47,52-2.5"})
        details = get_json(f"{base}/api/games/markets?{query}", timeout=30, headers=headers)
        for event_id, market_rows in (details.items() if isinstance(details, dict) else []):
            event = events_by_id.get(str(event_id))
            if not event:
                continue
            for market in market_rows:
                name = str(market.get("name", ""))
                selections = market.get("selections") or []
                if len(selections) != 2:
                    continue
                low = name.lower()
                if "both teams" in low:
                    canonical = "Both Teams to Score"
                    outcomes = [{"name": "yes" if "yes" in s.get("name", "").lower() else "no", "odds": s.get("odds")} for s in selections]
                elif "odd/even" in low:
                    canonical = "Odd/Even"
                    outcomes = [{"name": s.get("name", "").lower(), "odds": s.get("odds")} for s in selections]
                elif "over/under" in low:
                    canonical = total_market_name(name, market.get("specValue", ""), event["sport"])
                    outcomes = [{"name": "over" if s.get("shortName", "").upper() == "OV" else "under", "odds": s.get("odds")} for s in selections]
                elif "draw no bet" in low:
                    canonical = "Draw No Bet"
                    outcomes = [{"name": s.get("name"), "odds": s.get("odds")} for s in selections]
                else:
                    continue
                event["markets"].append({"name": canonical, "selections": outcomes})

    events = [event for event in events_by_id.values() if event["markets"]]
    result = payload(events)
    save_odds("sportpesa", result)
    return result


def collect_meridianbet() -> dict:
    """MeridianBet renders its sportsbook from a live WebSocket; read its own event-card DOM."""
    sport_routes = {
        "football": "Football", "basketball": "Basketball", "tennis": "Tennis",
        "table-tennis": "Table Tennis", "volleyball": "Volleyball", "handball": "Handball",
        "ice-hockey": "Ice Hockey", "american-football": "American Football", "baseball": "Baseball",
        "rugby-union": "Rugby Union", "cricket": "Cricket", "badminton": "Badminton", "snooker": "Snooker",
    }
    weekday = {"monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3, "friday": 4, "saturday": 5, "sunday": 6}
    local_now = datetime.now(timezone(timedelta(hours=3)))
    rows_by_key = {}
    with sync_playwright() as playwright:
        browser = launch_sync_browser(playwright)
        page = browser.new_page(locale="en-TZ", timezone_id="Africa/Dar_es_Salaam")
        for slug, sport_name in sport_routes.items():
            try:
                page.goto(f"https://meridianbet.co.tz/en/betting/{slug}", wait_until="domcontentloaded", timeout=60000)
                page.wait_for_selector(".c-event", timeout=25000)
                all_tab = page.locator(".c-event-filter__tabs .c-event-filter__tab").filter(has_text=re.compile(r"^All$", re.I))
                if all_tab.count():
                    all_tab.click(timeout=3000)
                    page.wait_for_timeout(900)
                page.wait_for_timeout(1800)
                # Meridian loads more event cards as the sportsbook page scrolls.
                for _ in range(4):
                    page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                    page.wait_for_timeout(450)
                cards = page.locator(".c-event").evaluate_all("""els => els.map(e => ({
                    live: !!e.querySelector('.c-event__period--live'),
                    teams: Array.from(e.querySelectorAll('.c-event__rivals--home,.c-event__rivals--away')).map(x => x.innerText.trim()),
                    when: (e.querySelector('.c-event__time')?.innerText || '').trim(),
                    url: e.querySelector('.c-event__info')?.getAttribute('href') || '',
                    groups: Array.from(e.querySelectorAll('.c-event__game-selections')).map(g => Array.from(g.querySelectorAll('.c-selection')).map(x => x.innerText.trim()))
                }))""")
            except Exception as error:
                print(f"MeridianBet {sport_name} page unavailable this cycle: {type(error).__name__}: {error}")
                continue
            for card in cards:
                if card["live"] or len(card["teams"]) != 2 or not all(card["teams"]):
                    continue
                # The rendered schedule labels are local time, Today/Tomorrow, or weekday names.
                label = card["when"].lower()
                if "tomorrow" in label:
                    day_offset = 1
                elif "today" in label:
                    day_offset = 0
                else:
                    day_offset = next(((weekday[name] - local_now.weekday()) % 7 for name in weekday if name in label), None)
                if day_offset is None or day_offset > 7:
                    continue
                clock_match = re.search(r"\b([01]?\d|2[0-3]):([0-5]\d)\b", card["when"])
                if not clock_match:
                    continue
                local_start = (local_now + timedelta(days=day_offset)).replace(
                    hour=int(clock_match.group(1)), minute=int(clock_match.group(2)), second=0, microsecond=0
                )
                markets = []
                for group_index, values in enumerate(card["groups"][:2]):
                    parsed = []
                    for value in values:
                        try:
                            number = float(value.replace(",", "."))
                        except (TypeError, ValueError):
                            parsed.append(None)
                            continue
                        parsed.append(number if number > 1 else None)
                    if group_index == 0 and len(parsed) == 3 and all(parsed):
                        markets.append({"name": "1x2", "selections": [{"name": card["teams"][0], "odds": parsed[0]}, {"name": "draw", "odds": parsed[1]}, {"name": card["teams"][1], "odds": parsed[2]}]})
                    elif group_index == 0 and len(parsed) == 2 and all(parsed):
                        markets.append({"name": "winner", "selections": [{"name": card["teams"][0], "odds": parsed[0]}, {"name": card["teams"][1], "odds": parsed[1]}]})
                    elif group_index == 1 and len(parsed) == 3 and parsed[0] and parsed[2]:
                        line = values[1] if len(values) > 1 else ""
                        # Meridian's rendered totals slots are Under, line, Over.
                        # Keep the displayed price attached to its actual side.
                        markets.append({"name": total_market_name("", line, sport_name), "selections": [{"name": "under", "odds": parsed[0]}, {"name": "over", "odds": parsed[2]}]})
                if markets:
                    key = card["url"] or f"{sport_name}:{card['teams'][0]}:{card['teams'][1]}"
                    rows_by_key[key] = {"name": f"{card['teams'][0]} vs {card['teams'][1]}", "sport": sport_name, "site": "meridianbet", "markets": markets, "startTime": local_start.astimezone(timezone.utc).isoformat()}
        browser.close()
    events = list(rows_by_key.values())
    result = payload(events)
    save_odds("meridianbet", result)
    return result


def collect_pigabet() -> dict:
    """PigaBet uses BetConstruct's sportsbook WebSocket; read its live event/market snapshots."""
    sports = {"Soccer": "Football", "Basketball": "Basketball", "Tennis": "Tennis",
              "Cricket": "Cricket", "Volleyball": "Volleyball"}
    games_by_id: dict[str, dict] = {}
    with sync_playwright() as playwright:
        browser = launch_sync_browser(playwright)
        page = browser.new_page(locale="en-TZ")

        def on_socket(socket):
            def on_frame(frame):
                try:
                    message = json.loads(str(frame))
                    if message.get("rid") != "get_initial_games":
                        return
                    sport_tree = message.get("data", {}).get("data", {}).get("sport", {})

                    def visit(node):
                        if isinstance(node, dict):
                            if "team1_name" in node and "team2_name" in node and "market" in node:
                                game_id = str(node.get("id") or "")
                                if game_id:
                                    games_by_id[game_id] = node
                                return
                            for value in node.values():
                                visit(value)
                        elif isinstance(node, list):
                            for value in node:
                                visit(value)
                    visit(sport_tree)
                except (TypeError, ValueError, json.JSONDecodeError):
                    return
            socket.on("framereceived", on_frame)

        page.on("websocket", on_socket)
        for alias, sport_name in sports.items():
            before = set(games_by_id)
            try:
                page.goto(f"https://www.pigabet.co.tz/en/sports?sport={alias}", wait_until="domcontentloaded", timeout=45000)
                page.wait_for_timeout(5000)
                if not (set(games_by_id) - before):
                    print(f"PigaBet {sport_name}: no sportsbook snapshot received this cycle.")
            except Exception as error:
                print(f"PigaBet {sport_name} feed unavailable this cycle: {type(error).__name__}: {error}")
        browser.close()

    now = time.time()
    events = []
    sport_names = {"Soccer": "Football", "Basketball": "Basketball", "Tennis": "Tennis", "Cricket": "Cricket", "Volleyball": "Volleyball"}
    for game in games_by_id.values():
        if game.get("is_live") or game.get("is_blocked"):
            continue
        try:
            start = int(game.get("start_ts") or 0)
            if start <= now or start > now + 7 * 86400:
                continue
        except (TypeError, ValueError, OverflowError):
            continue
        home, away = str(game.get("team1_name") or "").strip(), str(game.get("team2_name") or "").strip()
        if not home or not away:
            continue
        grouped: dict[tuple[str, str], dict[str, dict]] = {}
        for market in (game.get("market") or {}).values():
            if str(market.get("display_sub_key", "MATCH")).upper() != "MATCH":
                continue
            display = str(market.get("display_key", "")).upper()
            market_type = str(market.get("market_type", "")).lower()
            raw_outcomes = list((market.get("event") or {}).values())
            valid = []
            for outcome in raw_outcomes:
                try:
                    price = float(outcome.get("price") or 0)
                except (TypeError, ValueError):
                    continue
                if price > 1:
                    valid.append((outcome, price))
            if len(valid) not in (2, 3):
                continue
            if display == "WINNER" or market_type in {"matchresult", "moneyline"}:
                key = ("1x2" if len(valid) == 3 else "winner", "")
                labels = []
                for outcome, price in valid:
                    name = str(outcome.get("name", "")).strip().lower()
                    label = home if name in {"w1", "1", "home", home.lower()} else away if name in {"w2", "2", "away", away.lower()} else "draw" if name in {"x", "draw"} else str(outcome.get("name", "")).strip()
                    labels.append((label, price))
                if key[0] == "1x2" and {x[0] for x in labels} != {home, "draw", away}:
                    continue
                if key[0] == "winner" and {x[0] for x in labels} != {home, away}:
                    continue
                for label, price in labels:
                    grouped.setdefault(key, {})[label] = {"name": label, "odds": price}
            elif display == "TOTALS" or market_type in {"overunder", "total"}:
                line = market.get("base")
                line = f"{float(line):g}" if line not in (None, "") else ""
                key = ("totals", line)
                for outcome, price in valid:
                    label = str(outcome.get("name", "")).strip().lower()
                    if label.startswith("over"):
                        name = "over"
                    elif label.startswith("under"):
                        name = "under"
                    else:
                        continue
                    grouped.setdefault(key, {})[name] = {"name": name, "odds": price}
            elif display == "BOTHTEAMTOSCORE" or market_type == "bothteamstoscore":
                key = ("btts", "")
                for outcome, price in valid:
                    label = str(outcome.get("name", "")).strip().lower()
                    if label in {"yes", "no"}:
                        grouped.setdefault(key, {})[label] = {"name": label, "odds": price}
            elif display in {"ODD/EVEN", "ODD_EVEN"} or market_type in {"oddeven", "oddeven"}:
                key = ("odd_even", "")
                for outcome, price in valid:
                    label = str(outcome.get("name", "")).strip().lower()
                    if label.startswith("odd") or label.startswith("even"):
                        label = "odd" if label.startswith("odd") else "even"
                        grouped.setdefault(key, {})[label] = {"name": label, "odds": price}
        markets = []
        for (kind, line), outcomes in grouped.items():
            if kind in {"1x2", "winner"} and len(outcomes) == (3 if kind == "1x2" else 2):
                markets.append({"name": kind, "selections": list(outcomes.values())})
            elif kind in {"totals", "btts", "odd_even"} and len(outcomes) == 2:
                market_name = total_market_name("", line, sport_names.get(game.get("sport_alias"), game.get("sport_alias") or "")) if kind == "totals" else "Both Teams to Score" if kind == "btts" else "Odd/Even"
                markets.append({"name": market_name, "selections": list(outcomes.values())})
        if markets:
            events.append({"name": f"{home} vs {away}", "sport": sport_names.get(game.get("sport_alias"), game.get("sport_alias") or ""),
                           "site": "pigabet", "markets": markets, "startTime": datetime.fromtimestamp(start, timezone.utc).isoformat()})
    result = payload(events)
    save_odds("pigabet", result)
    return result


def collect_betpawa() -> dict:
    """BetPawa's old sports-api host is gone; collect its current localized event-card sportsbook feed."""
    categories = {"2": "Football", "3": "Basketball", "452": "Tennis"}
    now = datetime.now(timezone(timedelta(hours=3)))
    events_by_id = {}
    with sync_playwright() as playwright:
        browser = launch_sync_browser(playwright)
        page = browser.new_page(locale="en-TZ", timezone_id="Africa/Dar_es_Salaam")
        for category_id, sport_name in categories.items():
            try:
                page.goto(f"https://www.betpawa.co.tz/events?categoryId={category_id}", wait_until="domcontentloaded", timeout=60000)
                page.wait_for_selector('a[data-test-id="bp-event"]', timeout=30000)
                # The event list is lazy-loaded. Scroll the sport-specific page to expose more upcoming fixtures.
                event_cards = page.locator('a[data-test-id="bp-event"]')
                for _ in range(8):
                    event_cards.last.scroll_into_view_if_needed(timeout=3000)
                    page.wait_for_timeout(600)
                cards = page.locator('a[data-test-id="bp-event"]').evaluate_all("""els => els.map(e => ({
                    href: e.getAttribute('href') || '',
                    dateText: e.querySelector('[class*="SportEvents_eventMatch"] .text-sm')?.innerText.trim() || '',
                    teams: Array.from(e.querySelectorAll('[class*="ScoreBoard_participantName"]')).map(x => x.innerText.trim()),
                    markets: Array.from(e.querySelectorAll('[class*="CollapsibleMarkets_anyUpGroup"]')).map(g => ({
                        name: g.querySelector('[class*="CollapsibleMarkets_marketNameHeader"]')?.innerText.trim() || '',
                        selections: Array.from(g.querySelectorAll('button[data-test-id^="odd-"]')).map(b => ({
                            label: b.querySelector('span[class*="_label_"]')?.innerText.trim() || '',
                            odds: b.querySelector('span[class*="_oddsValue_"]')?.innerText.trim() || ''
                        }))
                    })),
                    directSelections: Array.from(e.querySelectorAll('button[data-test-id^="odd-"]')).map(b => ({
                        label: b.querySelector('span[class*="_label_"]')?.innerText.trim() || '',
                        odds: b.querySelector('span[class*="_oddsValue_"]')?.innerText.trim() || ''
                    }))
                }))""")
            except Exception as error:
                print(f"BetPawa {sport_name} page unavailable this cycle: {type(error).__name__}: {error}")
                continue
            for card in cards:
                match = re.search(r"/(?:event|events)/(\d+)", card["href"])
                if not match or len(card["teams"]) < 2 or not card["teams"][0] or not card["teams"][1]:
                    continue
                date_match = re.search(r"(\d{1,2}:\d{2}\s*[ap]m)\s*([A-Za-z]{3})\s*(\d{1,2}/\d{2})", card["dateText"], re.I)
                if not date_match:
                    continue
                start = None
                for year in (now.year, now.year + 1):
                    try:
                        start = datetime.strptime(f"{date_match.group(1)} {date_match.group(2)} {date_match.group(3)} {year}", "%I:%M %p %a %d/%m %Y").replace(tzinfo=now.tzinfo)
                        if start >= now - timedelta(days=1):
                            break
                    except ValueError:
                        start = None
                if not start or start < now or start > now + timedelta(days=7):
                    continue
                markets = []
                for market in card["markets"]:
                    label = market["name"].lower()
                    selections = []
                    for outcome in market["selections"]:
                        try:
                            price = float(outcome["odds"])
                        except (TypeError, ValueError):
                            continue
                        if price > 1 and outcome["label"]:
                            selections.append({"name": outcome["label"], "odds": price})
                    if "1x2" in label and "1up" not in label and "2up" not in label and len(selections) == 3:
                        mapped = []
                        for selection in selections:
                            token = selection["name"].strip().lower()
                            mapped.append({**selection, "name": "draw" if token in {"x", "draw"} else card["teams"][0] if token in {"1", "home"} else card["teams"][1] if token in {"2", "away"} else selection["name"]})
                        if {s["name"] for s in mapped} == {card["teams"][0], "draw", card["teams"][1]}:
                            markets.append({"name": "1x2", "selections": mapped})
                    elif any(term in label for term in ("winner", "moneyline", "match result")) and len(selections) == 2:
                        mapped = [{**s, "name": card["teams"][0] if s["name"].strip().lower() in {"1", "home"} else card["teams"][1] if s["name"].strip().lower() in {"2", "away"} else s["name"]} for s in selections]
                        markets.append({"name": "winner", "selections": mapped})
                    elif any(term in label for term in ("over/under", "total score", "total corner", "total goal", "total point")) and len(selections) == 2:
                        line = re.search(r"\d+(?:\.\d+)?", " ".join(s["name"] for s in selections))
                        if line:
                            mapped = [{**s, "name": "over" if s["name"].lower().startswith("over") else "under"} for s in selections]
                            markets.append({"name": total_market_name(label, line.group(0), sport_name), "selections": mapped})
                    elif "both teams" in label and len(selections) == 2:
                        markets.append({"name": "Both Teams to Score", "selections": [{**s, "name": s["name"].lower()} for s in selections]})
                    elif "odd/even" in label and len(selections) == 2:
                        markets.append({"name": "Odd/Even", "selections": [{**s, "name": s["name"].lower()} for s in selections]})
                if not markets and card["directSelections"]:
                    direct = []
                    for selection in card["directSelections"]:
                        try:
                            price = float(selection["odds"])
                        except (TypeError, ValueError):
                            continue
                        if price > 1 and selection["label"]:
                            direct.append({"name": selection["label"].strip(), "odds": price})
                    labels = {selection["name"].lower() for selection in direct}
                    if len(direct) == 3 and labels == {"1", "x", "2"}:
                        mapped = [{**s, "name": "draw" if s["name"].lower() == "x" else card["teams"][0] if s["name"] == "1" else card["teams"][1]} for s in direct]
                        markets.append({"name": "1x2", "selections": mapped})
                    elif len(direct) == 2 and labels in ({"1", "2"}, {"home", "away"}):
                        mapped = [{**s, "name": card["teams"][0] if s["name"].lower() in {"1", "home"} else card["teams"][1]} for s in direct]
                        markets.append({"name": "winner", "selections": mapped})
                if markets:
                    events_by_id[match.group(1)] = {"name": f"{card['teams'][0]} vs {card['teams'][1]}", "sport": sport_name,
                        "site": "betpawa", "markets": markets, "startTime": start.astimezone(timezone.utc).isoformat()}
        browser.close()
    events = list(events_by_id.values())
    result = payload(events)
    save_odds("betpawa", result)
    return result


def collect_sportybet() -> dict:
    events = []
    # IDs from SportyBet's public ordered sport list. Virtual sports are left
    # out; these are real sports and scheduled esports competitions.
    sports = {
        "sr%3Asport%3A1": "Football", "sr%3Asport%3A2": "Basketball", "sr%3Asport%3A5": "Tennis",
        "sr%3Asport%3A20": "Table Tennis", "sr%3Asport%3A4": "Ice Hockey", "sr%3Asport%3A6": "Handball",
        "sr%3Asport%3A23": "Volleyball", "sr%3Asport%3A3": "Baseball", "sr%3Asport%3A16": "American Football",
        "sr%3Asport%3A21": "Cricket", "sr%3Asport%3A22": "Darts", "sr%3Asport%3A117": "MMA",
        "sr%3Asport%3A10": "Boxing", "sr%3Asport%3A31": "Badminton", "sr%3Asport%3A34": "Beach Volleyball",
        "sr%3Asport%3A29": "Futsal", "sr%3Asport%3A12": "Rugby", "sr%3Asport%3A19": "Snooker",
        "sr%3Asport%3A155": "Basketball 3x3", "sr%3Asport%3A109": "Esports Counter-Strike",
        "sr%3Asport%3A111": "Esports Dota 2", "sr%3Asport%3A110": "Esports League of Legends",
    }
    market_ids = "1%2C18%2C10%2C14%2C29%2C11%2C26%2C36%2C60100"
    for sport_id, sport_name in sports.items():
        url = f"https://www.sportybet.com/api/tz/factsCenter/pcUpcomingEvents?sportId={sport_id}&marketId={market_ids}&pageSize=100&pageNum=1&option=1"
        for tournament in get_json(url).get("data", {}).get("tournaments", []):
            for item in tournament.get("events", []):
                home, away = item.get("homeTeamName"), item.get("awayTeamName")
                if not home or not away:
                    continue
                markets = []
                for market in item.get("markets", []):
                    raw_name = market.get("name", "")
                    outcomes = [outcome for outcome in market.get("outcomes", []) if outcome.get("isActive")]
                    if raw_name == "1X2" and len(outcomes) in {2, 3}:
                        labels = {"Home": home, "Draw": "draw", "Away": away, "1": home, "X": "draw", "2": away}
                        selections = [{"name": labels.get(outcome.get("desc"), outcome.get("desc")), "odds": outcome.get("odds")} for outcome in outcomes]
                        market_name = "1x2" if len(selections) == 3 and any(str(item["name"]).lower() == "draw" for item in selections) else "winner"
                        markets.append({"name": market_name, "selections": selections})
                    elif raw_name.lower() in {"winner", "match winner", "moneyline"} and len(outcomes) == 2:
                        labels = {"Home": home, "Away": away, "1": home, "2": away}
                        selections = [{"name": labels.get(outcome.get("desc"), outcome.get("desc")), "odds": outcome.get("odds")} for outcome in outcomes]
                        markets.append({"name": "winner", "selections": selections})
                    elif raw_name == "Over/Under" and len(outcomes) == 2:
                        line = market.get("specifier", "").split("=", 1)[-1]
                        markets.append({"name": total_market_name(raw_name, line, sport_name), "selections": [{"name": outcome.get("desc"), "odds": outcome.get("odds")} for outcome in outcomes]})
                    elif any(term in raw_name.lower() for term in ("handicap", "spread")) and len(outcomes) == 2:
                        line = market.get("specifier", "").split("=", 1)[-1]
                        selections = []
                        for outcome in outcomes:
                            label = str(outcome.get("desc", "")).replace("Home", home).replace("Away", away)
                            selections.append({"name": label, "odds": outcome.get("odds")})
                        markets.append({"name": f"Handicap {line}", "selections": selections})
                    elif any(term in raw_name.lower() for term in ("both teams to score", "btts", "odd/even", "odd even", "yes/no")) and len(outcomes) == 2:
                        selections = [{"name": outcome.get("desc"), "odds": outcome.get("odds")} for outcome in outcomes]
                        markets.append({"name": raw_name, "selections": selections})
                if markets:
                    events.append({"name": f"{home} vs {away}", "sport": sport_name, "site": "sportybet", "markets": markets})
    result = payload(events)
    save_odds("sportybet", result)
    return result


PUBLIC_API_COLLECTORS = (collect_betika, collect_leonbet, collect_premierbet, collect_22bet, collect_1xbet, collect_helabet, collect_betwinner, collect_megapari, collect_1xbit, collect_melbet, collect_sportpesa, collect_meridianbet, collect_pigabet, collect_betpawa, collect_sportybet, collect_wasafibet, collect_bangbet, collect_thronebet)


def run_public_api_collectors() -> list[str]:
    completed = []
    health = read_collector_health()
    bookmakers = health.setdefault("bookmakers", {})

    def run_one(collector):
        site = collector.__name__.removeprefix("collect_")
        try:
            return site, collector(), None
        except HTTPError as error:
            return site, None, f"HTTP {error.code}"
        except Exception as error:
            return site, None, f"{type(error).__name__}: {error}"

    # Independent bookmakers can be fetched concurrently. Eight site workers
    # shorten the sweep; each line-feed adapter uses fewer nested workers so
    # the aggregate request load remains bounded.
    # Each bookmaker has its own network/API path. Run them concurrently so a
    # slow browser-based collector does not leave already-fetched books stale
    # while the rest of the list waits for a worker slot.
    with ThreadPoolExecutor(max_workers=len(PUBLIC_API_COLLECTORS), thread_name_prefix="odds-feed") as executor:
        futures = [executor.submit(run_one, collector) for collector in PUBLIC_API_COLLECTORS]
        for future in as_completed(futures):
            site, result, error = future.result()
            attempted_at = datetime.now(timezone.utc).isoformat()
            entry = bookmakers.setdefault(site, {"family": bookmaker_family(site), "lastSuccessAt": None})
            entry.update({"family": bookmaker_family(site), "lastAttemptAt": attempted_at, "error": error})
            if error:
                entry["status"] = "error"
                print(f"Skipped {site} this cycle: {error}; will retry next cycle.")
            elif result and result["events"]:
                completed.append(site)
                entry.update({
                    "status": "ok",
                    "lastSuccessAt": attempted_at,
                    "eventCount": len(result["events"]),
                    "marketCount": sum(len(event.get("markets", [])) for event in result["events"]),
                })
            else:
                entry.update({"status": "no_data", "eventCount": 0, "marketCount": 0})
            health["updatedAt"] = attempted_at
            persist_collector_health(health)
    return completed
