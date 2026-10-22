"""Start backend API and refresh odds/arbitrage on a schedule."""

import subprocess
import sys
import threading
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def run_backend():
    print("Starting ArbRadar backend on http://127.0.0.1:8001")
    subprocess.run(
        [sys.executable, "-m", "uvicorn", "backend.main:app", "--host", "127.0.0.1", "--port", "8001", "--reload"],
        cwd=PROJECT_ROOT,
        check=False,
    )


def run_pipeline():
    backend_thread = threading.Thread(target=run_backend, daemon=True)
    backend_thread.start()
    print("Pipeline running. The API starts its collector and live engine workers automatically.")
    backend_thread.join()


if __name__ == "__main__":
    run_pipeline()
