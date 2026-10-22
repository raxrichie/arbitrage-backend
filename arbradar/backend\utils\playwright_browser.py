"""Launch Playwright with an installed browser when its bundled browser is absent."""


def launch_sync_browser(playwright, *, headless: bool = True):
    failures = []
    for channel in ("msedge", "chrome", None):
        try:
            options = {"headless": headless}
            if channel:
                options["channel"] = channel
            return playwright.chromium.launch(**options)
        except Exception as error:
            browser_name = channel or "Playwright Chromium"
            failures.append(f"{browser_name}: {error}")
    raise RuntimeError("Could not launch any supported browser. " + " | ".join(failures))


async def launch_async_browser(playwright, *, headless: bool = True):
    failures = []
    for channel in ("msedge", "chrome", None):
        try:
            options = {"headless": headless}
            if channel:
                options["channel"] = channel
            return await playwright.chromium.launch(**options)
        except Exception as error:
            browser_name = channel or "Playwright Chromium"
            failures.append(f"{browser_name}: {error}")
    raise RuntimeError("Could not launch any supported browser. " + " | ".join(failures))
