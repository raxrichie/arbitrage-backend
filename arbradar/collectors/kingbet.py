from playwright.sync_api import sync_playwright
from backend.utils.save import save_odds

def run():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/114 Safari/537.36',
            extra_http_headers={'Accept-Language':'sw-TZ,en;q=0.9','X-Forwarded-For':'196.10.10.10'}
        )
        page = context.new_page()

        for attempt in range(3):
            try:
                page.goto("https://www.kingbet.co.tz", timeout=90000, wait_until="domcontentloaded")
                page.wait_for_selector(".odd", timeout=90000)
                odds_elements = page.query_selector_all(".odd")
                scraped_data = {
                    "arbs": [],
                    "oddsChanges": [
                        {"match": el.inner_text(), "old": None, "new": el.inner_text()}
                        for el in odds_elements
                    ],
                    "marginHistory": []
                }
                save_odds("kingbet", scraped_data)
                print(f"? Odds saved for {site}")
                break
            except Exception as e:
                print(f"? Attempt {attempt+1} failed for {site}: {e}")
        browser.close()

if __name__ == "__main__":
    run()
