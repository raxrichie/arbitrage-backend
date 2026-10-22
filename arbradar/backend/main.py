from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
import asyncio
import json

from backend.services.arb_engine import ENGINE_REPORT, MAX_ARB_ODDS_AGE, MAX_ODDS_AGE, build_engine_data, write_engine_report
from backend.collectors.metadata import group_bookmakers

PROJECT_ROOT = Path(__file__).resolve().parents[1]
app = FastAPI()
_background_services_started = False
_engine_response_cache = None
_engine_response_cached_at = None
_engine_response_report_mtime = None

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173",
        "http://127.0.0.1:5173",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
def start_live_services():
    """Keep odds collection and the engine report running with the API."""
    global _background_services_started
    if _background_services_started:
        return
    from backend.runner.live_services import start_background_services
    _background_services_started = start_background_services()


def normalize_arb(arb: dict) -> dict:
    raw_margin = float(arb.get("margin", arb.get("profit", 0)) or 0)
    margin = raw_margin / 100 if raw_margin > 1 else raw_margin
    match_name = arb.get("match")
    if isinstance(match_name, dict):
        match_name = match_name.get("name", "")
    return {
        "margin": margin,
        "match": {
            "id": arb.get("id") or str(match_name or "").replace(" ", "_").lower(),
            "name": match_name,
        },
        "market": arb.get("market", "1x2"),
        "sport": arb.get("sport", ""),
        "selections": arb.get("selections", []),
    }


def merge_engine_payload(primary: dict, supplemental: dict) -> dict:
    now = datetime.now(timezone.utc)

    def is_fresh_arb(arb: dict) -> bool:
        selections = arb.get("selections") or []
        if not selections:
            return False
        for selection in selections:
            observed_at = selection.get("observedAt")
            if not observed_at:
                return False
            try:
                observed = datetime.fromisoformat(observed_at.replace("Z", "+00:00"))
            except (TypeError, ValueError):
                return False
            if observed.tzinfo is None:
                observed = observed.replace(tzinfo=timezone.utc)
            if now - observed > MAX_ARB_ODDS_AGE:
                return False
        return True

    # The scanner report can be behind a collector cycle. Recheck every quote
    # at response time so stale paths never remain actionable in the UI.
    candidates = primary.get("arbs", []) or supplemental.get("arbs", [])
    arbs = [normalize_arb(arb) for arb in candidates if is_fresh_arb(arb)]
    near_misses = [
        normalize_arb(arb)
        for arb in (primary.get("nearMisses") or supplemental.get("nearMisses") or [])
        if is_fresh_arb(arb)
    ]
    diagnostics = dict(primary.get("diagnostics") or supplemental.get("diagnostics") or {})
    if candidates and not arbs:
        diagnostics["opportunities"] = 0
        diagnostics["reason"] = (
            f"The last detected paths have quotes older than {int(MAX_ARB_ODDS_AGE.total_seconds())} seconds; "
            "waiting for refreshed bookmaker odds."
        )

    odds_changes = primary.get("oddsChanges") or supplemental.get("oddsChanges") or []
    margin_history = primary.get("marginHistory") or supplemental.get("marginHistory") or []

    sources = list(dict.fromkeys((primary.get("sources") or []) + (supplemental.get("sources") or [])))
    source_count = primary.get("sourceCount")
    if source_count is None:
        source_count = len(sources) or supplemental.get("sourceCount", 0)
    selection_times = [
        selection.get("observedAt")
        for arb in arbs
        for selection in (arb.get("selections") or [])
        if selection.get("observedAt")
    ]
    try:
        with (PROJECT_ROOT / "reports" / "collector-health.json").open(encoding="utf-8") as handle:
            collector_health = json.load(handle)
    except (OSError, json.JSONDecodeError):
        collector_health = {"updatedAt": None, "bookmakers": {}}
    opportunity_history = primary.get("opportunityHistory")
    if not opportunity_history:
        try:
            with (PROJECT_ROOT / "reports" / "engine" / "opportunity-history.json").open(encoding="utf-8") as handle:
                opportunity_history = json.load(handle)
        except (OSError, json.JSONDecodeError):
            opportunity_history = []

    return {
        "arbs": arbs,
        "oddsChanges": odds_changes,
        "marginHistory": margin_history,
        "sourceCount": source_count,
        "sources": sources,
        "diagnostics": diagnostics,
        "nearMisses": near_misses,
        "sourceFamilies": group_bookmakers(sources),
        "collectorHealth": collector_health,
        "opportunityHistory": opportunity_history or [],
        "updatedAt": primary.get("updatedAt"),
        "lastOddsAt": primary.get("lastOddsAt") or (max(selection_times) if selection_times else None),
        "oldestOddsAt": primary.get("oldestOddsAt") or (min(selection_times) if selection_times else None),
        "oldestCurrentArbOddsAt": min(selection_times) if selection_times else None,
    }


def load_site_reports() -> dict:
    base_dir = PROJECT_ROOT / "reports"
    arbs = []
    odds_changes = []
    margin_history = []
    active_sources = []
    today = datetime.now().date()

    for site in base_dir.glob("*/odds/latest.json"):
        if site.parents[1].name == "engine":
            continue
        try:
            modified = datetime.fromtimestamp(site.stat().st_mtime, tz=timezone.utc)
            if modified.date() != today or datetime.now(timezone.utc) - modified > MAX_ODDS_AGE:
                continue

            with site.open(encoding="utf-8") as handle:
                data = json.load(handle)

            if not any(data.get(key) for key in ("arbs", "oddsChanges", "marginHistory", "events")):
                continue

            active_sources.append(site.parents[1].name)

            for arb in data.get("arbs", []):
                arbs.append(normalize_arb(arb))

            odds_changes.extend(data.get("oddsChanges", []))
            margin_history.extend(data.get("marginHistory", []))
        except Exception as error:
            print("Error reading", site, error)

    return {
        "arbs": arbs,
        "oddsChanges": odds_changes,
        "marginHistory": margin_history,
        "sourceCount": len(active_sources),
        "sources": active_sources,
    }


def build_engine_response() -> dict:
    global _engine_response_cache, _engine_response_cached_at, _engine_response_report_mtime
    now = datetime.now(timezone.utc)
    try:
        report_mtime = ENGINE_REPORT.stat().st_mtime_ns
    except FileNotFoundError:
        report_mtime = None

    # The collector pipeline refreshes this report after each data cycle. Cache
    # the assembled payload between cycles so the live stream does not rescan
    # thousands of odds files every three seconds.
    if (
        _engine_response_cache is not None
        and report_mtime == _engine_response_report_mtime
        and _engine_response_cached_at is not None
        and now - _engine_response_cached_at < timedelta(seconds=3)
    ):
        return _engine_response_cache

    if report_mtime is not None:
        modified = datetime.fromtimestamp(ENGINE_REPORT.stat().st_mtime, tz=timezone.utc)
    else:
        modified = None
    if modified is not None and now - modified <= MAX_ARB_ODDS_AGE:
        try:
            with ENGINE_REPORT.open(encoding="utf-8") as handle:
                primary = json.load(handle)
        except (OSError, json.JSONDecodeError):
            primary = None
    else:
        primary = None

    if primary is None:
        # Keep the live request path cheap. The background scanner owns full
        # odds parsing; if its report is stale, tell the UI that plainly
        # instead of rebuilding tens of thousands of rows inside each SSE
        # request.
        primary = {
            "arbs": [],
            "oddsChanges": [],
            "marginHistory": [],
            "sourceCount": 0,
            "sources": [],
            "diagnostics": {
                "status": "stale",
                "reason": "The engine scan is stale; waiting for the background scanner to refresh bookmaker odds.",
                "oddsRows": 0,
                "sources": [],
                "matchingMarkets": 0,
                "opportunities": 0,
            },
            "nearMisses": [],
            "updatedAt": modified.isoformat() if modified else None,
            "lastOddsAt": None,
            "oldestOddsAt": None,
            "oldestCurrentArbOddsAt": None,
        }

    supplemental = load_site_reports()
    _engine_response_cache = merge_engine_payload(primary, supplemental)
    _engine_response_cached_at = now
    _engine_response_report_mtime = report_mtime
    return _engine_response_cache


@app.get("/engine")
def get_engine():
    return build_engine_response()


@app.get("/engine/live")
async def stream_engine():
    async def event_stream():
        while True:
            payload = json.dumps(build_engine_response())
            yield f"event: engine\ndata: {payload}\n\n"
            await asyncio.sleep(3)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/health")
def health_check():
    return {"status": "ok", "time": datetime.now(timezone.utc).isoformat()}


# The cloud image builds the React dashboard into dist/ and serves it from the
# same origin as the API, so the live event stream works without a separate host.
FRONTEND_BUILD = PROJECT_ROOT / "dist"
if FRONTEND_BUILD.is_dir():
    app.mount("/", StaticFiles(directory=FRONTEND_BUILD, html=True), name="frontend")
