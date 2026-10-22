"""Validate the configured bookmaker pages without writing any odds reports.

Run from the project root:
    .\\.venv\\Scripts\\python.exe -m backend.runner.validate_collectors
"""

import asyncio
import json
import re
from pathlib import Path

from playwright.async_api import async_playwright

from backend.collectors.scraper import parse_samples
from backend.collectors.sites import SITES
from backend.utils.playwright_browser import launch_async_browser


PROJECT_ROOT = Path(__file__).resolve().parents[2]
COLLECTORS_DIR = PROJECT_ROOT / "collectors"
RESULT_FILE = PROJECT_ROOT / "reports" / "collector-validation.json"
COMMON_ODDS_SELECTORS = (
    ".c-bets__odd",
    ".odd",
    "[data-testid*='odd']",
    "[data-qa*='odd']",
    "[class*='odd']",
)


def configured_sites():
    """Validate every licensed sportsbook, rather than only legacy scripts."""
    for site, brand, url in SITES:
        yield {"site": site, "brand": brand, "url": url, "selector": "[class*='odd']"}


async def validate_site(browser, config, semaphore):
    async with semaphore:
        page = await browser.new_page()
        try:
            response = await page.goto(config["url"], wait_until="domcontentloaded", timeout=15_000)
            await page.wait_for_timeout(1_000)
            selector_counts = {}
            for selector in dict.fromkeys((config["selector"], *COMMON_ODDS_SELECTORS)):
                selector_counts[selector] = await page.locator(selector).count()

            body = (await page.locator("body").inner_text(timeout=5_000)).lower()
            blocked = any(token in body for token in ("access denied", "just a moment", "captcha", "unusual traffic"))
            working_selector = next((key for key, count in selector_counts.items() if count), None)
            samples = []
            if working_selector:
                samples = await page.locator(working_selector).all_text_contents()
                samples = [sample.strip() for sample in samples if sample.strip()][:8]
            sports_events = parse_samples(samples)
            return {
                **config,
                # A class containing "odd" is not sufficient: it can be a
                # virtual-game or page-control node.  Require a complete,
                # parsed sports 1x2 event before the collector may run.
                "status": "ready" if sports_events and not blocked else "needs_collector_work",
                "httpStatus": response.status if response else None,
                "workingSelector": working_selector,
                "selectorCounts": selector_counts,
                "samples": samples,
                "parsedSportsEvents": len(sports_events),
                "blocked": blocked,
                "title": await page.title(),
            }
        except Exception as error:
            return {**config, "status": "unreachable", "error": str(error)}
        finally:
            await page.close()


async def main():
    sites = list(configured_sites())
    async with async_playwright() as playwright:
        browser = await launch_async_browser(playwright)
        semaphore = asyncio.Semaphore(8)
        results = await asyncio.gather(*(validate_site(browser, site, semaphore) for site in sites))
        await browser.close()

    RESULT_FILE.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    for result in results:
        detail = result.get("workingSelector") or result.get("error", "no odds selector matched")
        print(f"{result['site']}: {result['status']} — {detail}")


if __name__ == "__main__":
    asyncio.run(main())
