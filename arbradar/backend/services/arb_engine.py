"""Collect odds from Web Audit + local reports, scan for arbitrage, write engine output."""

from __future__ import annotations

import csv
import hashlib
import json
import os
import re
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

from backend.utils.atomic import replace_with_retry

PROJECT_ROOT = Path(__file__).resolve().parents[2]
WEB_AUDIT_REPORTS = PROJECT_ROOT.parent / "Web Audit" / "reports"
ENGINE_REPORT = PROJECT_ROOT / "reports" / "engine" / "latest.json"
OPPORTUNITY_HISTORY_REPORT = PROJECT_ROOT / "reports" / "engine" / "opportunity-history.json"
ARBRADAR_REPORTS = PROJECT_ROOT / "reports"
_REPORT_LOCK = threading.Lock()
_ODDS_FILE_CACHE: dict[str, tuple[int, list[dict]]] = {}

TEAM_SYNONYMS = {
    "barca": "barcelona",
    "fc barcelona": "barcelona",
    "man utd": "manchester united",
    "man united": "manchester united",
    "juve": "juventus",
    "yanga": "young africans",
    "young africans dar es salaam": "young africans",
    "simba sc": "simba",
}

DRAW_ALIASES = {"draw", "x", "tie", "d"}
TWO_OUTCOME_MARKETS = {"12", "winner", "moneyline"}
TWO_WAY_MARKETS = TWO_OUTCOME_MARKETS | {"over under", "handicap"}
YES_NO_MARKETS = {"yes no", "both teams to score", "btts", "odd even"}
MAX_ODDS_AGE = timedelta(minutes=15)
MAX_ARB_ODDS_AGE = timedelta(seconds=90)
NON_EVENT_TERMS = {"odds", "onyesha", "alama zilizoboreshwa", "chini ya", "markets"}
SIMULATED_EVENT_TERMS = (
    "virtual", "simulated", "simulation", "simulator", "e-soccer", "esoccer",
    "e-football", "efootball", "e-basketball", "ebasketball", "e-tennis", "etennis",
    "virtual sports", "cyber", "esports", "e-sports", "v-football", "v football",
    "fifae", "fifa esports", "fifa e-sports",
)


def fixture_compliance_checks() -> list[dict]:
    """Report which requested data-quality protections are enforced today."""
    return [
        {"name": "Virtual and simulated event labels", "status": "partial", "detail": "Filters common virtual, esports, cyber, and simulation labels in event, sport, league, and competition text. FIFAe is excluded; ordinary FIFA competitions remain eligible."},
        {"name": "Virtual URL paths and provider tags", "status": "missing", "detail": "Collector records do not consistently retain endpoint paths or provider identifiers for filtering."},
        {"name": "Compressed fixture schedule detection", "status": "missing", "detail": "No cross-event cadence analysis is applied; start times are not consistently available."},
        {"name": "Suspicious match ID patterns", "status": "missing", "detail": "Match IDs are not retained and profiled across feeds."},
        {"name": "Odds and market sanity", "status": "partial", "detail": "Odds range, market identity, outcome completeness, and quote freshness are checked. Overround behavior and robotic update patterns are not analyzed."},
        {"name": "Canonical participant identity", "status": "partial", "detail": "A small synonym map is used; there is no master participant ID registry."},
        {"name": "Venue cross-reference", "status": "missing", "detail": "Venue data is not collected or verified."},
        {"name": "Cross-book event matching", "status": "partial", "detail": "Normalized participant names must match exactly after limited aliases; no fuzzy confidence score or kickoff-time check is used."},
        {"name": "Odds movement anomaly flags", "status": "missing", "detail": "Large unexplained price changes are not flagged against score or match-state changes."},
        {"name": "Manual review queue", "status": "missing", "detail": "Ambiguous or suspicious fixtures are dropped or processed by existing rules; there is no review queue."},
    ]


def is_fresh(path: Path) -> bool:
    """Only use recently collected prices; stale prices are unsafe for arbitrage."""
    modified = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
    return datetime.now(timezone.utc) - modified <= MAX_ODDS_AGE


def is_identified_event(event_name: object, market: object, selections: object) -> bool:
    """Reject page controls/promotions that look like an event to a loose parser."""
    if not isinstance(event_name, str) or not isinstance(market, dict) or not isinstance(selections, list):
        return False
    if " vs " not in event_name.lower() or any(term in event_name.lower() for term in NON_EVENT_TERMS):
        return False
    home, away = (part.strip() for part in event_name.lower().split(" vs ", 1))
    if not home or not away or not any(char.isalpha() for char in home) or not any(char.isalpha() for char in away):
        return False
    market_name = normalize_name(str(market.get("name") or "1x2"))
    is_over_under = ("over" in market_name and "under" in market_name) or "total" in market_name
    is_handicap = "handicap" in market_name
    is_binary = (
        market_name in YES_NO_MARKETS
        or "yes" in market_name and "no" in market_name
        or "both teams to score" in market_name
        or "odd" in market_name and "even" in market_name
    )
    expected_length = 3 if market_name == "1x2" else 2 if market_name in TWO_OUTCOME_MARKETS or is_over_under or is_handicap or is_binary else 0
    if len(selections) != expected_length:
        return False
    return all(
        isinstance(selection, dict)
        and isinstance(selection.get("name"), str)
        and (selection["name"].lower() in DRAW_ALIASES or any(char.isalpha() for char in selection["name"]))
        and not any(term in selection["name"].lower() for term in NON_EVENT_TERMS)
        for selection in selections
    )


def is_real_sports_event(event: dict) -> bool:
    searchable = " ".join((
        str(event.get("name", "")), str(event.get("sport", "")),
        str(event.get("league", "")), str(event.get("competition", "")),
        str(event.get("category", "")), str(event.get("tournament", "")),
        str(event.get("provider", "")), str(event.get("providerName", "")),
        str(event.get("url", "")), str(event.get("eventUrl", "")),
    )).casefold()
    return not any(term in searchable for term in SIMULATED_EVENT_TERMS)


def is_live_quote(row: dict, now: datetime | None = None) -> bool:
    try:
        observed = datetime.fromisoformat(row.get("observedAt", ""))
    except (TypeError, ValueError):
        return False
    return (now or datetime.now(timezone.utc)) - observed <= MAX_ARB_ODDS_AGE


def event_start_time(event: dict) -> str | None:
    """Return a validated fixture start time when a feed provides one."""
    for key in ("startTime", "start_time", "commence_time", "commenceTime", "startDate", "start_date", "kickoff", "kickoffTime", "date"):
        value = event.get(key)
        if value in (None, ""):
            continue
        try:
            if isinstance(value, (int, float)) or str(value).isdigit():
                stamp = float(value)
                if stamp > 10_000_000_000:
                    stamp /= 1000
                parsed = datetime.fromtimestamp(stamp, tz=timezone.utc)
            else:
                parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.astimezone(timezone.utc).isoformat()
        except (TypeError, ValueError, OverflowError, OSError):
            continue
    return None


def normalize_name(name: str) -> str:
    if not name:
        return ""
    lowered = name.lower().strip()
    # Tennis feeds frequently alternate between "Surname, Given" and
    # "Given Surname". Put comma-delimited person names in the same order.
    if "," in lowered:
        parts = [part.strip() for part in lowered.split(",") if part.strip()]
        if len(parts) == 2:
            lowered = f"{parts[1]} {parts[0]}"
    for synonym, canonical in TEAM_SYNONYMS.items():
        if synonym in lowered:
            return canonical
    cleaned = re.sub(r"[^a-z0-9 ]", "", lowered)
    return re.sub(r"\s+", " ", cleaned).strip()


def normalize_sport(name: object) -> str:
    value = normalize_name(str(name or ""))
    return {
        "soccer": "football",
        "soka": "football",
        "association football": "football",
        "mpira wa kikapu": "basketball",
        "tennis mezani": "table tennis",
    }.get(value, value)


def normalize_event(name: object) -> str:
    value = str(name or "")
    parts = value.split(" vs ", 1)
    if len(parts) != 2:
        return normalize_name(value)
    home, away = (normalize_name(part) for part in parts)
    return " vs ".join(sorted((home, away)))


def normalize_market(name: object, sport: object = "") -> str:
    """Keep total stat, line and period in the market identity."""
    raw_market = str(name or "1x2").lower()
    market = normalize_name(raw_market) or "1x2"
    if ("over" in market and "under" in market) or "total" in market:
        line = re.search(r"\d+(?:[.,]\d+)?", raw_market)
        stat_aliases = (
            ("corners", ("corner",)), ("goals", ("goal",)), ("points", ("point",)),
            ("runs", ("run",)), ("games", ("game",)), ("sets", ("set",)),
            ("cards", ("card",)), ("shots", ("shot",)), ("aces", ("ace",)),
        )
        stat = next((label for label, tokens in stat_aliases if any(re.search(rf"\b{re.escape(token)}s?\b", market) for token in tokens)), None)
        if stat is None:
            if "score" in market and normalize_sport(sport) in {"football", "soccer", "handball", "ice hockey"}:
                stat = "goals"
        if stat is None and "score" in market:
            stat = "points"
        if stat is None:
            stat = {
                "football": "goals", "soccer": "goals", "basketball": "points", "baseball": "runs",
                "ice hockey": "goals", "handball": "goals", "tennis": "games", "table tennis": "points",
                "volleyball": "points", "rugby": "points",
            }.get(normalize_sport(sport), "unspecified total")
        line_text = f"{float(line.group().replace(',', '.')):g}" if line else "unspecified line"
        period = ""
        for pattern, label in (
            (r"\b(first half|1st half|half time|1h)\b", " first half"),
            (r"\b(second half|2nd half|2h)\b", " second half"),
            (r"\b(first quarter|1st quarter|q1)\b", " first quarter"),
            (r"\b(second quarter|2nd quarter|q2)\b", " second quarter"),
            (r"\b(first set|set 1)\b", " first set"),
            (r"\b(full time|full match|match total|game total|ft|90 minutes)\b", " full match"),
        ):
            if re.search(pattern, raw_market):
                period = label
                break
        return f"total {stat} over under {line_text}{period}".strip()
    if "handicap" in market or "spread" in market:
        line = re.search(r"[-+]?\d+(?:[.,]\d+)?", raw_market)
        normalized_line = abs(float(line.group().replace(',', '.'))) if line else ""
        category = "corners" if "corner" in market else "goals" if "goal" in market or normalize_sport(sport) in {"football", "soccer", "handball", "ice hockey"} else "points"
        return f"handicap {category} {normalized_line:g}".strip() if line else f"handicap {category}"
    if market in {"btts", "both teams to score"}:
        return "both teams to score"
    if "odd" in market and "even" in market:
        return "odd even"
    if "yes" in market and "no" in market:
        return "yes no"
    return market


def market_label(market: str) -> str:
    match = re.fullmatch(r"total (.+?) over under (.+?)(?: (first half|second half|first quarter|second quarter|first set|full match))?", market)
    if match:
        stat, line, period = match.groups()
        return f"Total {stat.title()} · Over/Under {line} · {(period or 'period unspecified').title()}"
    return market.replace("_", " ").title()


def normalize_selection(name: object, market: str) -> str:
    raw_value = str(name or "").strip()
    if market.startswith("handicap"):
        # Keep each participant's signed spread with its selection. This lets
        # the scanner distinguish the complementary sides of the same line.
        line = re.search(r"\s*\(?\s*([+-])\s*(\d+(?:[.,]\d+)?)\s*\)?$", raw_value)
        if line:
            participant = raw_value[:line.start()].strip().rstrip("(").strip()
            signed_line = float(line.group(2).replace(",", ".")) * (1 if line.group(1) == "+" else -1)
            return f"{normalize_name(participant)} {signed_line:+g}"
    value = normalize_name(raw_value)
    if "over under" in market:
        return "over" if value.startswith("over") else "under" if value.startswith("under") else value
    if market.startswith("handicap"):
        return re.sub(r"\s[-+]?\d+(?:[.,]\d+)?$", "", value).strip()
    if value in {"yes", "y"}:
        return "yes"
    if value in {"no", "n"}:
        return "no"
    if value.startswith("odd"):
        return "odd"
    if value.startswith("even"):
        return "even"
    return value


def is_odds_json(data: object) -> bool:
    if isinstance(data, dict):
        if "events" in data and isinstance(data["events"], list):
            return True
        if "data" in data and isinstance(data["data"], list):
            first = data["data"][0] if data["data"] else {}
            return isinstance(first, dict) and ("home_team" in first or "match_id" in first)
        if "items" in data and isinstance(data["items"], list):
            return True
        if "Matches" in data:
            return True
        if "Value" in data and isinstance(data["Value"], list):
            return True
    if isinstance(data, list) and data and isinstance(data[0], dict):
        return "markets" in data[0] or "home_team" in data[0]
    return False


def extract_events(data: object) -> list[dict]:
    if isinstance(data, dict) and "events" in data:
        return [event for event in data["events"] if isinstance(event, dict)]
    # The Web Audit collector stores bookmaker API responses in ``data``.  Convert
    # that format here instead of guessing matches from rendered page text.
    if isinstance(data, dict) and isinstance(data.get("data"), list):
        events = []
        for item in data["data"]:
            if not isinstance(item, dict):
                continue
            home, away = item.get("home_team"), item.get("away_team")
            if not home or not away:
                continue
            markets = []
            for market in item.get("odds", []):
                selections = []
                for selection in market.get("odds", []):
                    if not isinstance(selection, dict):
                        continue
                    selections.append(
                        {
                            "name": selection.get("display_name") or selection.get("display"),
                            "odds": selection.get("odd_value"),
                        }
                    )
                markets.append({"name": market.get("name") or "1x2", "selections": selections})
            events.append({"name": f"{home} vs {away}", "sport": item.get("sport") or item.get("sport_name", ""), "markets": markets, "startTime": event_start_time(item)})
        return events
    if isinstance(data, list):
        return [event for event in data if isinstance(event, dict)]
    return []


def _cached_json_odds_rows(json_file: Path) -> list[dict]:
    """Parse a sportsbook snapshot once, refreshing its rows when the file changes."""
    try:
        stat = json_file.stat()
        cache_key = str(json_file.resolve())
        cached = _ODDS_FILE_CACHE.get(cache_key)
        if cached and cached[0] == stat.st_mtime_ns:
            return cached[1]

        data = json.loads(json_file.read_text(encoding="utf-8"))
        rows = []
        if is_odds_json(data):
            site = json_file.parents[1].name
            observed_at = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat()
            for event in extract_events(data):
                if not is_real_sports_event(event):
                    continue
                event_name = event.get("name", "")
                for market in event.get("markets", []):
                    market_name = market.get("name", "")
                    selections = market.get("selections", [])
                    if not is_identified_event(event_name, market, selections):
                        continue
                    for selection in selections:
                        odds = selection.get("odds")
                        if odds in (None, ""):
                            continue
                        rows.append({
                            "event": event_name,
                            "sport": event.get("sport", ""),
                            "market": market_name or "1x2",
                            "selection": selection.get("name", ""),
                            "site": site,
                            "odds": float(odds),
                            "observedAt": observed_at,
                            "startTime": event_start_time(event),
                        })
        _ODDS_FILE_CACHE[cache_key] = (stat.st_mtime_ns, rows)
        return rows
    except Exception:
        return []


def _cached_master_csv_rows(master_csv: Path) -> list[dict]:
    try:
        stat = master_csv.stat()
        cache_key = str(master_csv.resolve())
        cached = _ODDS_FILE_CACHE.get(cache_key)
        if cached and cached[0] == stat.st_mtime_ns:
            return cached[1]

        rows = []
        observed_at = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat()
        with master_csv.open(encoding="utf-8", newline="") as handle:
            for row in csv.DictReader(handle):
                try:
                    csv_searchable = " ".join(str(row.get(field, "")) for field in ("event", "sport", "league", "competition")).casefold()
                    if any(term in csv_searchable for term in SIMULATED_EVENT_TERMS):
                        continue
                    rows.append({
                        "event": row["event"],
                        "sport": row.get("sport", ""),
                        "market": row.get("market") or "1x2",
                        "selection": row["selection"],
                        "site": row["site"],
                        "odds": float(row["odds"]),
                        "observedAt": observed_at,
                        "startTime": event_start_time(row),
                    })
                except (KeyError, TypeError, ValueError):
                    continue
        _ODDS_FILE_CACHE[cache_key] = (stat.st_mtime_ns, rows)
        return rows
    except Exception:
        return []


def collect_odds_rows() -> list[dict]:
    rows: list[dict] = []

    # 1) Web Audit structured odds JSON files
    if WEB_AUDIT_REPORTS.exists():
        for site_folder in WEB_AUDIT_REPORTS.iterdir():
            if not site_folder.is_dir():
                continue
            odds_dir = site_folder / "odds"
            if not odds_dir.exists():
                continue
            for json_file in odds_dir.glob("*.json"):
                if is_fresh(json_file):
                    rows.extend(_cached_json_odds_rows(json_file))

    # 2) Web Audit master_odds.csv fallback
    master_csv = WEB_AUDIT_REPORTS / "master_odds.csv"
    if master_csv.exists() and is_fresh(master_csv):
        rows.extend(_cached_master_csv_rows(master_csv))

    # 3) ArbRadar per-site reports with events or oddsChanges
    if ARBRADAR_REPORTS.exists():
        for json_file in ARBRADAR_REPORTS.glob("*/odds/latest.json"):
            if is_fresh(json_file):
                rows.extend(_cached_json_odds_rows(json_file))

    return rows


def scan_arbitrage(rows: list[dict], include_nonpositive: bool = False) -> list[dict]:
    if not rows:
        return []

    grouped: dict[tuple[str, str, str], dict[str, dict]] = {}
    for row in rows:
        event_norm = normalize_event(row["event"])
        market_norm = normalize_market(row.get("market") or "1x2", row.get("sport"))
        is_over_under = market_norm.startswith("total ") and "over under" in market_norm
        is_handicap = market_norm.startswith("handicap")
        is_binary = market_norm in YES_NO_MARKETS or "yes no" in market_norm or "both teams to score" in market_norm or "odd even" in market_norm
        if not (
            market_norm == "1x2"
            or market_norm in TWO_OUTCOME_MARKETS
            or is_over_under and "unspecified line" not in market_norm
            or is_handicap
            or is_binary
        ):
            continue
        selection_norm = normalize_selection(row["selection"], market_norm)
        if not event_norm or not selection_norm:
            continue
        try:
            odds = float(row["odds"])
        except (TypeError, ValueError):
            continue
        if not 1.01 <= odds <= 1_000:
            continue

        sport_norm = normalize_sport(row.get("sport"))
        key = (sport_norm, event_norm, market_norm)
        best = grouped.setdefault(key, {})
        current = best.get(selection_norm)
        if not current or odds > current["odds"]:
            best[selection_norm] = {
                "selection": row["selection"],
                "site": row["site"],
                "odds": odds,
                "event": row["event"],
                "market": row.get("market") or "1x2",
                "observedAt": row.get("observedAt"),
                "startTime": row.get("startTime"),
            }

    opportunities = []
    for (sport_norm, event_norm, market_norm), selections in grouped.items():
        if len(selections) < 2:
            continue
        # A 1x2 market requires all three outcomes. Two-outcome markets are
        # valid only when explicitly labelled as winner/moneyline/12.
        if market_norm == "1x2" and len(selections) != 3:
            continue
        if (market_norm in TWO_OUTCOME_MARKETS or is_over_under or is_handicap or is_binary) and len(selections) != 2:
            continue
        if market_norm not in {"1x2", *TWO_OUTCOME_MARKETS, *YES_NO_MARKETS} and not is_over_under and not is_handicap and not is_binary:
            continue
        if market_norm == "1x2":
            teams = event_norm.split(" vs ", 1)
            if len(teams) != 2:
                continue
            expected = {teams[0], teams[1], "draw"}
            if set(selections) != expected:
                continue
        if market_norm in TWO_OUTCOME_MARKETS:
            teams = event_norm.split(" vs ", 1)
            if len(teams) != 2 or set(selections) != set(teams):
                continue
        if is_over_under and set(selections) != {"over", "under"}:
            continue
        if is_handicap:
            teams = event_norm.split(" vs ", 1)
            parsed = []
            for selection in selections:
                match = re.match(r"^(.*?)\s+([+-]\d+(?:\.\d+)?)$", selection)
                if not match:
                    parsed = []
                    break
                parsed.append((match.group(1), float(match.group(2))))
            if (
                len(teams) != 2
                or len(parsed) != 2
                or {participant for participant, _ in parsed} != set(teams)
                or abs(sum(line for _, line in parsed)) > 0.000001
            ):
                continue
        if is_binary and set(selections) not in ({"yes", "no"}, {"odd", "even"}):
            continue
        if len({pick["site"] for pick in selections.values()}) < 2:
            continue
        # A plausible margin built from an old quote is not a current
        # opportunity. Keep it out of the live list; the raw feed snapshots
        # remain available for diagnostics/history.
        now = datetime.now(timezone.utc)
        try:
            if any(
                not pick.get("observedAt")
                or now - datetime.fromisoformat(pick["observedAt"]) > MAX_ARB_ODDS_AGE
                for pick in selections.values()
            ):
                continue
        except (TypeError, ValueError):
            continue
        implied_prob = sum(1 / pick["odds"] for pick in selections.values())
        if implied_prob >= 1 and not include_nonpositive:
            continue

        profit_margin = (1 - implied_prob) * 100
        display_event = next(iter(selections.values()))["event"] or event_norm.replace("_", " ").title()
        opportunities.append(
            {
                "match": display_event,
                "id": event_norm,
                "profit": profit_margin,
                "margin": profit_margin / 100,
                "market": market_label(market_norm),
                "sport": sport_norm,
                "startTime": next((pick.get("startTime") for pick in selections.values() if pick.get("startTime")), None),
                "selections": list(selections.values()),
            }
        )

    opportunities.sort(key=lambda item: item["profit"], reverse=True)
    return opportunities


def build_odds_changes(rows: list[dict], limit: int = 40) -> list[dict]:
    changes = []
    for row in rows[-limit:]:
        changes.append(
            {
                "match": f"{row['event']} — {row['selection']} ({row['site']})",
                "old": None,
                "new": row["odds"],
            }
        )
    return changes


def build_margin_history(opportunities: list[dict], limit: int = 30) -> list[dict]:
    now = datetime.now(timezone.utc).isoformat()
    history = []
    for opportunity in opportunities[:limit]:
        history.append(
            {
                "timestamp": now,
                "margin": opportunity["profit"],
            }
        )
    return history


def build_diagnostics(
    rows: list[dict],
    opportunities: list[dict],
    checked_markets: list[dict] | None = None,
    rows_are_live: bool = False,
) -> dict:
    live_rows = rows if rows_are_live else [row for row in rows if is_live_quote(row)]
    sources = sorted({row["site"] for row in live_rows})
    sites_by_market: dict[tuple[str, str, str], set[str]] = {}
    for row in live_rows:
        key = (
            normalize_sport(row.get("sport")),
            normalize_event(row["event"]),
            normalize_market(row.get("market"), row.get("sport")),
        )
        sites_by_market.setdefault(key, set()).add(row["site"])
    cross_book_markets = sum(len(sites) > 1 for sites in sites_by_market.values())
    checked_markets = checked_markets if checked_markets is not None else scan_arbitrage(live_rows, include_nonpositive=True)
    negative_margins = [item["profit"] for item in checked_markets if item["profit"] < 0]
    closest_margin = max(negative_margins) if negative_margins else None
    if not live_rows:
        reason = f"No verified odds were updated within the last {int(MAX_ARB_ODDS_AGE.total_seconds())} seconds."
    elif len(sources) < 2:
        reason = "Only one bookmaker has verified current markets; arbitrage requires at least two."
    elif not cross_book_markets:
        reason = "Bookmakers have current odds, but no matching event and market pairs yet."
    elif not opportunities:
        suffix = f" Closest checked margin: {closest_margin:.2f}% (needs to exceed 0%)." if closest_margin is not None else ""
        reason = "Matching cross-book markets were checked, but none have a positive arbitrage margin." + suffix
    else:
        reason = f"{len(opportunities)} verified cross-book opportunities are available."
    return {"status": "ok", "reason": reason, "oddsRows": len(live_rows), "sources": sources, "matchingMarkets": cross_book_markets, "closestMargin": closest_margin, "opportunities": len(opportunities)}


def build_engine_data() -> dict:
    rows = [row for row in collect_odds_rows() if is_live_quote(row)]
    # One complete market pass supplies positive paths, near misses, and the
    # diagnostic margin. Repeating this scan over every bookmaker feed made
    # each live refresh needlessly CPU-heavy.
    checked_markets = scan_arbitrage(rows, include_nonpositive=True)
    opportunities = [item for item in checked_markets if item["profit"] > 0]
    near_misses = [item for item in checked_markets if item["profit"] < 0][:10]
    sources = sorted({row["site"] for row in rows if is_live_quote(row)})

    arbs = [
        {
            "match": opportunity["match"],
            "id": opportunity["id"],
            "profit": opportunity["profit"],
            "margin": opportunity["margin"],
            "market": opportunity["market"],
            "sport": opportunity["sport"],
            "selections": opportunity["selections"],
        }
        for opportunity in opportunities
    ]

    return {
        "arbs": arbs,
        "oddsChanges": build_odds_changes(rows),
        "marginHistory": build_margin_history(opportunities),
        "sourceCount": len(sources),
        "sources": sources,
        "diagnostics": build_diagnostics(rows, opportunities, checked_markets, rows_are_live=True),
        "complianceChecks": fixture_compliance_checks(),
        "nearMisses": near_misses,
        "updatedAt": datetime.now(timezone.utc).isoformat(),
        "lastOddsAt": max((row.get("observedAt", "") for row in rows), default=None) or None,
        "oldestOddsAt": min((row.get("observedAt", "") for row in rows), default=None) or None,
        "oldestCurrentArbOddsAt": min(
            (selection.get("observedAt", "") for arb in arbs for selection in arb.get("selections", [])),
            default=None,
        ) or None,
    }


def record_opportunity_history(opportunities: list[dict]) -> list[dict]:
    """Persist each distinct observed odds state so fleeting paths remain reviewable."""
    try:
        history = json.loads(OPPORTUNITY_HISTORY_REPORT.read_text(encoding="utf-8"))
        if not isinstance(history, list):
            history = []
    except (OSError, json.JSONDecodeError):
        history = []

    now = datetime.now(timezone.utc).isoformat()
    latest_by_key = {}
    for index, entry in enumerate(history):
        latest_by_key[entry.get("key")] = index

    for opportunity in opportunities:
        selections = sorted(
            ({"selection": row.get("selection"), "site": row.get("site"), "odds": row.get("odds")}
             for row in opportunity.get("selections", [])),
            key=lambda row: (str(row["site"]), str(row["selection"])),
        )
        key_payload = {"match": opportunity.get("id"), "market": opportunity.get("market"), "selections": selections}
        key = hashlib.sha256(json.dumps(key_payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:20]
        prior_index = latest_by_key.get(key)
        if prior_index is not None and history[prior_index].get("selections") == selections:
            history[prior_index]["lastSeenAt"] = now
            history[prior_index]["observations"] = int(history[prior_index].get("observations", 0)) + 1
            history[prior_index]["margin"] = opportunity.get("profit", 0)
            continue
        entry = {
            "key": key,
            "match": opportunity.get("match"),
            "sport": opportunity.get("sport"),
            "market": opportunity.get("market"),
            "margin": opportunity.get("profit", 0),
            "selections": selections,
            "firstSeenAt": now,
            "lastSeenAt": now,
            "observations": 1,
        }
        history.append(entry)
        latest_by_key[key] = len(history) - 1

    history = history[-5000:]
    OPPORTUNITY_HISTORY_REPORT.parent.mkdir(parents=True, exist_ok=True)
    temporary = OPPORTUNITY_HISTORY_REPORT.with_name(f"opportunity-history.{os.getpid()}.{threading.get_ident()}.tmp")
    temporary.write_text(json.dumps(history, ensure_ascii=False, indent=2), encoding="utf-8")
    replace_with_retry(temporary, OPPORTUNITY_HISTORY_REPORT)
    return history[-500:]


def write_engine_report() -> dict:
    with _REPORT_LOCK:
        payload = build_engine_data()
        history = record_opportunity_history(payload["arbs"])
        payload["opportunityHistory"] = history
        payload["marginHistory"] = [
            {"timestamp": entry["lastSeenAt"], "margin": entry["margin"]}
            for entry in history[-300:]
        ]
        ENGINE_REPORT.parent.mkdir(parents=True, exist_ok=True)
        temporary = ENGINE_REPORT.with_name(f"latest.{os.getpid()}.{threading.get_ident()}.tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        replace_with_retry(temporary, ENGINE_REPORT)
    print(
        f"Engine report written: {len(payload['arbs'])} arbs, "
        f"{payload['sourceCount']} sources -> {ENGINE_REPORT}"
    )
    return payload


if __name__ == "__main__":
    write_engine_report()
