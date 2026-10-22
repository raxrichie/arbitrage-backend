import json
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
REPORTS_DIR = PROJECT_ROOT.parent / "Web Audit" / "reports"
OUTPUT_FILE = Path(__file__).resolve().parent / "selectors.json"


def build_selectors():
    selectors = {}
    if REPORTS_DIR.exists():
        for site_dir in REPORTS_DIR.iterdir():
            audit_path = site_dir / "audit.json"
            if not audit_path.exists():
                continue
            try:
                with audit_path.open("r", encoding="utf-8") as handle:
                    audit = json.load(handle)
                if "selectors" in audit:
                    selectors[site_dir.name] = {
                        "url": audit.get("url", f"https://{site_dir.name}.co.tz"),
                        "match": audit["selectors"].get("match", ""),
                        "teams": audit["selectors"].get("teams", ""),
                        "odds": audit["selectors"].get("odds", ""),
                    }
            except Exception as error:
                print(f"Failed to parse {audit_path}: {error}")
    with OUTPUT_FILE.open("w", encoding="utf-8") as handle:
        json.dump(selectors, handle, indent=2)
    return selectors


if __name__ == "__main__":
    build_selectors()
    print(f"selectors.json updated at {OUTPUT_FILE}")
