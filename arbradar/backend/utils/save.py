import json
from pathlib import Path

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
    temporary = path / f"latest.{__import__('os').getpid()}.{__import__('threading').get_ident()}.tmp"
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(odds_data, handle, ensure_ascii=False, indent=2)
    temporary.replace(file)
    print(f"Odds saved for {site}")
