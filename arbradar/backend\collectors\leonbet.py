from playwright.sync_api import sync_playwright
from backend.collector import save_odds

def run():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/114 Safari/537.36',
            extra_http_headers={'Accept-Language':'sw-TZ,en;q=0.9','X-Forwarded-For':'196.10.10.10'}
        )
        page = context.new_page()
        url = "<SITE_URL>"
        selector = "<CSS_SELECTOR>"

        for attempt in range(3):
            try:
                page.goto(url, timeout=90000, wait_until="domcontentloaded")
                page.wait_for_selector(selector, timeout=90000)
                odds_elements = page.query_selector_all(selector)
                scraped_data = {
                    "arbs": [],
                    "oddsChanges": [
                        {"match": el.inner_text(), "old": None, "new": el.inner_text()}
                        for el in odds_elements
                    ],
                    "marginHistory": []
                }
                save_odds("<SITE_NAME>", scraped_data)
                print(f"? Odds saved for <SITE_NAME>")
                break
            except Exception as e:
                print(f"? Attempt {attempt+1} failed for <SITE_NAME>: {e}")
        browser.close()

if __name__ == "__main__":
    run()
