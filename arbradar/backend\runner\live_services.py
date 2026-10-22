"""Background collector and engine loops shared by API and standalone startup."""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
LOCK_PATH = PROJECT_ROOT / "reports" / "engine" / "live-services.lock"
COLLECTION_INTERVAL_SECONDS = 60
ENGINE_SCAN_INTERVAL_SECONDS = 3
_lock_handle = None
_started = False
_guard = threading.Lock()


def _acquire_process_lock() -> bool:
    """Prevent duplicate collectors if the API and standalone runner both start."""
    global _lock_handle
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    _lock_handle = LOCK_PATH.open("a+b")
    _lock_handle.seek(0, os.SEEK_END)
    if _lock_handle.tell() == 0:
        _lock_handle.write(b"\0")
        _lock_handle.flush()
    _lock_handle.seek(0)
    try:
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(_lock_handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(_lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        _lock_handle.close()
        _lock_handle = None
        return False


def start_background_services() -> bool:
    global _started
    with _guard:
        if _started:
            return True
        if not _acquire_process_lock():
            print("ArbRadar background services already run in another process.")
            return False
        _started = True

    def collect_loop():
        from backend.runner.run_collectors import run_cycle

        while True:
            started_at = time.monotonic()
            try:
                run_cycle()
            except Exception as error:
                print(f"Live collector cycle failed: {type(error).__name__}: {error}")
            time.sleep(max(2, COLLECTION_INTERVAL_SECONDS - (time.monotonic() - started_at)))

    def report_loop():
        from backend.services.arb_engine import write_engine_report

        while True:
            try:
                write_engine_report()
            except Exception as error:
                print(f"Live engine refresh failed: {type(error).__name__}: {error}")
            time.sleep(ENGINE_SCAN_INTERVAL_SECONDS)

    threading.Thread(target=collect_loop, name="arbradar-collectors", daemon=True).start()
    threading.Thread(target=report_loop, name="arbradar-engine", daemon=True).start()
    print("ArbRadar live collection started: concurrent bookmaker feeds, 60-second cycle target, 3-second engine scans.")
    return True


if __name__ == "__main__":
    start_background_services()
    while True:
        time.sleep(60)
