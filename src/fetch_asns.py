"""
Step 1: fetch the list of registered ASNs for each country via RIPEstat (no API key needed).
Output: data/asn_cache/country_asns_<CC>.json  (persistent cache, reused across runs)
        data/checkpoints/fetch_asns_done.json  (resume checkpoint)

Usage:
    python src/fetch_asns.py --region southeast_asia
    python src/fetch_asns.py --region all
"""
import argparse
import json
import re
import sys
from pathlib import Path

import requests
import yaml

sys.path.insert(0, str(Path(__file__).parent))
from utils.cache import JSONCache
from utils.rate_limit import polite_sleep, Checkpoint
from utils.http import get_with_retry

ROOT = Path(__file__).resolve().parent.parent
RIPESTAT_URL = "https://stat.ripe.net/data/country-asns/data.json"


def load_config():
    with open(ROOT / "config.yml", "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_region_countries(region: str):
    """Returns a list of (region_name, country_code) pairs from the data/regions files."""
    regions_dir = ROOT / "data" / "regions"
    result = []
    files = list(regions_dir.glob("*.json")) if region == "all" else [regions_dir / f"{region}.json"]
    for fp in files:
        if not fp.exists():
            continue
        with open(fp, "r", encoding="utf-8") as f:
            content = json.load(f)
        rname = content["region"]
        if "countries" in content:
            for cc in content["countries"]:
                result.append((rname, cc))
        elif "sub_groups" in content:
            for sub, countries in content["sub_groups"].items():
                for cc in countries:
                    result.append((f"{rname}_{sub}", cc))
    return result


def _clean_asn(value) -> str:
    """
    Normalizes a single ASN value to its plain numeric string. RIPEstat's lod=1
    ("most detailed") response has been observed to wrap at least some entries as
    e.g. "AsnSingle(215040)" instead of a plain "215040" — confirmed against the
    live API, not a hypothetical. Extracting the first run of digits is robust to
    that wrapper (and to a value that's already clean — a plain "215040" or 215040
    passes through unchanged).
    """
    match = re.search(r"\d+", str(value))
    return match.group(0) if match else str(value)


def _extract_asns(routed_field):
    """
    Handles several possible shapes of the 'routed' field in the RIPEstat response:
    - comma-separated string: "1103,1104,2914"
    - list of numbers/strings: [1103, 1104]
    - list of objects: [{"asn": 1103}, ...]
    Every extracted value is passed through _clean_asn() — see its docstring for why.
    """
    if not routed_field:
        return []
    if isinstance(routed_field, str):
        return [_clean_asn(x.strip()) for x in routed_field.split(",") if x.strip()]
    asns = []
    for item in routed_field:
        if isinstance(item, dict):
            val = item.get("asn") or item.get("resource")
            if val is not None:
                asns.append(_clean_asn(val))
        else:
            asns.append(_clean_asn(item))
    return asns


def fetch_country_asns(session, cc: str, delay: float):
    """Fetches the registered (routed) ASNs for a given country via RIPEstat."""
    params = {"resource": cc, "lod": 1}
    try:
        resp = get_with_retry(session, RIPESTAT_URL, params=params, timeout=30)
        data = resp.json()
    finally:
        polite_sleep(delay)  # always pace, even on failure — don't hammer a struggling API
    asns = []
    for entry in data.get("data", {}).get("countries", []):
        asns.extend(_extract_asns(entry.get("routed")))
    return asns


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--region", default="all", help="region name or 'all'")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--max-countries", type=int, default=None)
    args = parser.parse_args()

    cfg = load_config()
    cache = JSONCache(ROOT / cfg["cache"]["dir"], cfg["cache"]["ttl_days"])
    ckpt = Checkpoint(ROOT / "data" / "checkpoints" / "fetch_asns_done.json")

    pairs = load_region_countries(args.region)
    max_c = args.max_countries or cfg["batch"]["max_countries_per_run"]

    session = requests.Session()
    session.headers.update({"User-Agent": "telecom-core-ranges-bot/1.0"})

    processed_this_run = 0
    for region_name, cc in pairs:
        ckpt_key = f"asns_{cc}"
        if args.resume and ckpt.is_done(ckpt_key):
            continue
        if processed_this_run >= max_c:
            print(f"[paused] processed {max_c} countries in this run, continue later with the same command (--resume)")
            break

        cache_key = f"country_asns_{cc}"
        cached = cache.get(cache_key)
        if cached is not None:
            print(f"[cache] {cc}: {len(cached)} ASN (from cache)")
        else:
            try:
                asns = fetch_country_asns(session, cc, cfg["rate_limit"]["ripestat_delay_seconds"])
                cache.set(cache_key, asns)
                print(f"[fetched] {cc}: {len(asns)} ASN")
            except Exception as e:
                print(f"[error] failed to fetch ASNs for country {cc}: {e}", file=sys.stderr)
                continue

        ckpt.mark_done(ckpt_key)
        processed_this_run += 1

    print(f"done: processed {processed_this_run} new countries in this run.")


if __name__ == "__main__":
    main()
