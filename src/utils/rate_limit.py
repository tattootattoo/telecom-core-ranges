"""
Helper utilities: delay between requests + a simple checkpoint system to support
resuming, so no script re-processes what was already processed in a previous run
if it gets interrupted or times out.
"""
import json
import time
from pathlib import Path


def polite_sleep(seconds: float):
    time.sleep(seconds)


class Checkpoint:
    """Stores the list of items (ASN or country) successfully processed in a simple JSON file."""

    def __init__(self, path: str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.done = set()
        if self.path.exists():
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    self.done = set(json.load(f))
            except (json.JSONDecodeError, OSError):
                self.done = set()

    def is_done(self, item_id: str) -> bool:
        return item_id in self.done

    def mark_done(self, item_id: str):
        self.done.add(item_id)
        self._flush()

    def _flush(self):
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(sorted(self.done), f, ensure_ascii=False)
