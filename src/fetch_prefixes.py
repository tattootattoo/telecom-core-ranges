"""
Step 3: fetch the announced prefixes for every ASN classified as Core/Backbone only.
Source: RIPEstat announced-prefixes (no key needed).
Output: data/asn_cache/prefixes_<ASN>.json

Usage:
    python src/fetch_prefixes.py --region southeast_asia --resume
"""
import argparse
import sys
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).parent))
from utils.cache import JSONCache
from utils.rate_limit import polite_sleep, Checkpoint
from utils.http import get_with_retry
from fetch_asns import load_region_countries, load_config

ROOT = Path(__file__).resolve().parent.parent
RIPESTAT_PREFIXES_URL = "https://stat.ripe.net/data/announced-prefixes/data.json"


def fetch_prefixes(session, asn: str, delay: float):
    try:
        resp = get_with_retry(session, RIPESTAT_PREFIXES_URL, params={"resource": f"AS{asn}"}, timeout=30)
        data = resp.json()
    finally:
        polite_sleep(delay)  # always pace, even on failure — don't hammer a struggling API
    prefixes = [p["prefix"] for p in data.get("data", {}).get("prefixes", [])]
    return prefixes


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--region", default="all")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--max-asns", type=int, default=None)
    args = parser.parse_args()

    cfg = load_config()
    cache = JSONCache(ROOT / cfg["cache"]["dir"], cfg["cache"]["ttl_days"])
    ckpt = Checkpoint(ROOT / "data" / "checkpoints" / "prefixes_done.json")

    pairs = load_region_countries(args.region)
    max_asns = args.max_asns or cfg["batch"]["max_asns_per_run"]

    session = requests.Session()
    session.headers.update({"User-Agent": "telecom-core-ranges-bot/1.0"})

    processed = 0
    seen_asns = set()
    for region_name, cc in pairs:
        classified = cache.get(f"classified_{cc}") or {}
        for asn, info in classified.items():
            if not info.get("accepted"):
                continue
            if asn in seen_asns:
                continue
            seen_asns.add(asn)

            ckpt_key = f"pfx_{asn}"
            if args.resume and ckpt.is_done(ckpt_key):
                continue
            if processed >= max_asns:
                print(f"[paused] reached the limit of {max_asns} ASNs. Continue later with --resume")
                print(f"stopped early after processing {processed} ASNs.")
                return

            cache_key = f"prefixes_{asn}"
            if cache.get(cache_key) is not None:
                ckpt.mark_done(ckpt_key)
                continue

            try:
                prefixes = fetch_prefixes(session, asn, cfg["rate_limit"]["ripestat_delay_seconds"])
                cache.set(cache_key, prefixes)
                print(f"[{cc}] AS{asn} ({info.get('name')}) -> {len(prefixes)} prefixes")
            except Exception as e:
                print(f"[error] failed to fetch prefixes for AS{asn}: {e}", file=sys.stderr)
                continue

            ckpt.mark_done(ckpt_key)
            processed += 1

    print(f"done: fetched prefixes for {processed} new ASNs in this run.")


if __name__ == "__main__":
    main()
