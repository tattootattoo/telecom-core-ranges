"""
Simple JSON-file-based cache layer.
Purpose: avoid re-querying the same ASN / country before the cache entry expires,
which reduces run time and the number of requests over the long run.
"""
import json
import os
import time
from pathlib import Path


class JSONCache:
    def __init__(self, cache_dir: str, ttl_days: int = 30):
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.ttl_seconds = ttl_days * 86400

    def _path(self, key: str) -> Path:
        safe_key = key.replace("/", "_").replace(":", "_")
        return self.cache_dir / f"{safe_key}.json"

    def get(self, key: str):
        p = self._path(key)
        if not p.exists():
            return None
        try:
            with open(p, "r", encoding="utf-8") as f:
                entry = json.load(f)
        except (json.JSONDecodeError, OSError):
            return None
        if time.time() - entry.get("_ts", 0) > self.ttl_seconds:
            return None
        return entry.get("data")

    def set(self, key: str, data):
        p = self._path(key)
        with open(p, "w", encoding="utf-8") as f:
            json.dump({"_ts": time.time(), "data": data}, f, ensure_ascii=False)

    def exists_fresh(self, key: str) -> bool:
        return self.get(key) is not None
