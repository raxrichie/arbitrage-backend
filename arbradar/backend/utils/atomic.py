"""Small retry helper for atomic report replacement on Windows."""

from pathlib import Path
import time


def replace_with_retry(source: Path, destination: Path, attempts: int = 6) -> None:
    """Atomically replace a report, retrying brief Windows reader locks."""
    for attempt in range(attempts):
        try:
            source.replace(destination)
            return
        except PermissionError:
            if attempt + 1 == attempts:
                raise
            time.sleep(0.02 * (2 ** attempt))
