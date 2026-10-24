import json
from pathlib import Path

from backend.utils.atomic import replace_with_retry
from backend.collectors.sites import SITE_IDS

PROJECT_ROOT = Path(__file__).resolve().parents[1]

BOOKMAKERS = SITE_IDS


def save_odds(site: str, odds_data: dict):
    """Save odds data into reports/<site>/odds/latest.json"""
    if site not in BOOKMAKERS:
        raise ValueError(f"Unknown bookmaker: {site}")
    path = PROJECT_ROOT / "reports" / site / "odds"
    path.mkdir(parents=True, exist_ok=True)
    file = path / "latest.json"
    temporary = file.with_suffix(".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(odds_data, handle, ensure_ascii=False, indent=2)
    replace_with_retry(temporary, file)
    print(f"Odds saved for {site}")
