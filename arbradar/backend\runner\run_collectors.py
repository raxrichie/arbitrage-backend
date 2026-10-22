"""Run ready collectors, then refresh the arbitrage engine report."""

from backend.collectors.scraper import run_ready_collectors
from backend.collectors.public_api import run_public_api_collectors
from backend.services.arb_engine import write_engine_report


def run_cycle():
    sites = run_public_api_collectors() + run_ready_collectors()
    payload = write_engine_report()
    print(f"Cycle complete. Collectors: {len(sites)}, arbs: {len(payload['arbs'])}")
    return payload


if __name__ == "__main__":
    run_cycle()
