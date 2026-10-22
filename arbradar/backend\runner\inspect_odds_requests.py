"""Inspect public XHR/fetch traffic from each licensed sportsbook.

This intentionally uses a standard headless browser with no proxy, stealth
plugins, CAPTCHA handling, or access-control bypasses.  It records only public
responses that declare JSON, so site-specific collectors can use documented or
otherwise publicly served market data rather than brittle DOM guesses.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from playwright.async_api import async_playwright

from backend.collectors.sites import SITES
from backend.utils.playwright_browser import launch_async_browser

PROJECT_ROOT = Path(__file__).resolve().parents[2]
RESULT_FILE = PROJECT_ROOT / "reports" / "odds-request-inspection.json"


async def inspect_site(browser, site: str, brand: str, url: str, semaphore: asyncio.Semaphore) -> dict:
    async with semaphore:
        page = await browser.new_page(viewport={"width": 1440, "height": 900}, locale="en-TZ")
        candidates: list[dict] = []
        tasks: list[asyncio.Task] = []

        async def capture(response):
            request = response.request
            if request.resource_type not in {"xhr", "fetch"}:
                return
            content_type = response.headers.get("content-type", "").lower()
            if "json" not in content_type:
                return
            try:
                payload = await response.json()
            except Exception:
                return
            if isinstance(payload, dict):
                keys = list(payload)[:12]
                size = len(payload)
            elif isinstance(payload, list):
                keys = ["list"]
                size = len(payload)
            else:
                return
            url_lower = response.url.lower()
            serialized = json.dumps(payload, ensure_ascii=False)[:10_000].lower()
            odds_hint = any(token in url_lower or token in serialized for token in ("odd", "market", "fixture", "event", "sport"))
            if odds_hint:
                candidates.append({"url": response.url, "status": response.status, "keys": keys, "items": size})

        def on_response(response):
            tasks.append(asyncio.create_task(capture(response)))

        page.on("response", on_response)
        try:
            response = await page.goto(url, wait_until="domcontentloaded", timeout=20_000)
            await page.wait_for_timeout(5_000)
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            unique = list({candidate["url"]: candidate for candidate in candidates}.values())
            return {
                "site": site,
                "brand": brand,
                "url": url,
                "status": "inspected",
                "httpStatus": response.status if response else None,
                "oddsRequests": unique[:20],
            }
        except Exception as error:
            return {"site": site, "brand": brand, "url": url, "status": "unreachable", "error": str(error)}
        finally:
            await page.close()


async def main():
    async with async_playwright() as playwright:
        browser = await launch_async_browser(playwright)
        semaphore = asyncio.Semaphore(6)
        results = await asyncio.gather(
            *(inspect_site(browser, site, brand, url, semaphore) for site, brand, url in SITES)
        )
        await browser.close()
    RESULT_FILE.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    for result in results:
        print(f"{result['site']}: {len(result.get('oddsRequests', []))} public JSON market candidates")


if __name__ == "__main__":
    asyncio.run(main())
