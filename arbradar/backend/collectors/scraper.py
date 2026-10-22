"""Generic Playwright scraper for bookmaker sites."""

from __future__ import annotations

import json
import re
from pathlib import Path

from playwright.sync_api import sync_playwright

from backend.collector import save_odds
from backend.utils.playwright_browser import launch_sync_browser

PROJECT_ROOT = Path(__file__).resolve().parents[2]
VALIDATION_FILE = PROJECT_ROOT / "reports" / "collector-validation.json"

ODDS_RE = re.compile(r"^\d+(?:[.,]\d{1,2})$")
IGNORED_LABELS = {"odds", "show odds", "onyesha odds", "odds chini ya", "more", "markets"}


def parse_odd(value: str) -> float | None:
    value = value.strip().replace(",", ".")
    if not ODDS_RE.fullmatch(value):
        return None
    odd = float(value)
    return odd if 1.01 <= odd <= 1_000 else None


def load_ready_sites() -> list[dict]:
    if not VALIDATION_FILE.exists():
        return []
    results = json.loads(VALIDATION_FILE.read_text(encoding="utf-8"))
    return [
        site
        for site in results
        if site.get("status") == "ready" and site.get("workingSelector")
    ]


def parse_samples(samples: list[str]) -> list[dict]:
    """Best-effort parse of scraped text blocks into simple 1x2 events."""
    events = []
    for sample in samples:
        lines = [line.strip() for line in sample.splitlines() if line.strip()]
        pairs = []
        for index in range(len(lines) - 1):
            odd = parse_odd(lines[index + 1])
            label = lines[index].strip()
            if odd is not None and label.lower() not in IGNORED_LABELS and not parse_odd(label):
                pairs.append((label, odd))

        # We only publish a market when the page supplied an identifiable event
        # and all 1x2 outcomes together.  Isolated price nodes are real prices,
        # but cannot safely be compared across bookmakers.
        if len(pairs) == 3 and pairs[1][0].lower() in {"x", "draw", "tie"}:
            home, away = pairs[0][0], pairs[2][0]
            if home.lower() not in IGNORED_LABELS and away.lower() not in IGNORED_LABELS:
                event_name = f"{home} vs {away}"
                selections = [
                    {"name": home, "odds": pairs[0][1]},
                    {"name": "draw", "odds": pairs[1][1]},
                    {"name": away, "odds": pairs[2][1]},
                ]
                events.append(
                    {
                        "name": event_name,
                        "markets": [{"name": "1x2", "selections": selections}],
                    }
                )
    return events


def scrape_site(site: str, url: str, selector: str) -> dict:
    odds_changes = []
    events = []

    with sync_playwright() as playwright:
        browser = launch_sync_browser(playwright)
        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"
            ),
            extra_http_headers={"Accept-Language": "sw-TZ,en;q=0.9"},
        )
        page = context.new_page()

        for attempt in range(1):
            try:
                page.goto(url, timeout=30_000, wait_until="domcontentloaded")
                # Odds nodes can be present while an animation keeps their first
                # match "not visible".  Attached DOM content is enough to read
                # the public sports market safely.
                page.wait_for_selector(selector, state="attached", timeout=10_000)
                page.wait_for_timeout(1_000)
                elements = page.query_selector_all(selector)
                samples = [element.inner_text().strip() for element in elements if element.inner_text().strip()]
                events = parse_samples(samples[:12])

                for event in events:
                    for selection in event["markets"][0]["selections"]:
                        odds_changes.append(
                            {
                                "match": f"{event['name']} — {selection['name']} ({site})",
                                "old": None,
                                "new": selection["odds"],
                            }
                        )

                payload = {
                    "events": events,
                    "arbs": [],
                    "oddsChanges": odds_changes,
                    "marginHistory": [],
                }
                save_odds(site, payload)
                print(f"Scraped {site}: {len(events)} events, {len(odds_changes)} odds updates")
                break
            except Exception as error:
                print(f"Attempt {attempt + 1} failed for {site}: {str(error).encode('ascii', 'backslashreplace').decode()}")
        browser.close()

    return {
        "events": events,
        "arbs": [],
        "oddsChanges": odds_changes,
        "marginHistory": [],
    }


def run_ready_collectors() -> list[str]:
    completed = []
    # These sites have structured public API collectors; never overwrite their
    # richer API data with a DOM fallback.
    api_sites = {"betika", "leonbet", "premierbet", "22bet", "1xbet", "helabet", "betwinner", "megapari", "1xbit", "melbet", "sportpesa", "meridianbet", "pigabet", "betpawa", "sportybet", "wasafibet", "bangbet", "thronebet"}
    for site in load_ready_sites():
        if site["site"] in api_sites:
            continue
        scrape_site(site["site"], site["url"], site["workingSelector"])
        completed.append(site["site"])
    return completed


if __name__ == "__main__":
    run_ready_collectors()
