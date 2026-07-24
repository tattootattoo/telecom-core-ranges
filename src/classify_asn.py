"""
Step 2: classify each fetched ASN as Core/Backbone or User-facing, with a 0-100
confidence score (not just a binary decision).
Source: PeeringDB (no key needed for public reads, with a reasonable request limit).
Output: data/asn_cache/classified_<CC>.json
        { asn: {name, info_type, org_id, accepted: bool, score: int|None, reason} }

Usage:
    python src/classify_asn.py --region southeast_asia --resume
"""
import argparse
import json
import os
import sys
from pathlib import Path

import requests
import yaml

sys.path.insert(0, str(Path(__file__).parent))
from utils.cache import JSONCache
from utils.rate_limit import polite_sleep, Checkpoint
from utils.http import get_with_retry
from fetch_asns import load_region_countries, load_config

ROOT = Path(__file__).resolve().parent.parent
PEERINGDB_NET_URL = "https://www.peeringdb.com/api/net"


def classify_one(session, asn: str, cfg, delay: float):
    """
    Queries PeeringDB for a single ASN and returns a classification decision plus a
    0-100 confidence score reflecting how strong the evidence is (not just a binary
    accept/reject). `accepted` is kept for backward compatibility — it's just
    `score >= ACCEPT_THRESHOLD`. `score` is None for lookup errors: a failed request is
    a missing measurement, not evidence of low confidence, so it's kept out of the
    numeric scale entirely rather than defaulting to 0.
    """
    try:
        resp = get_with_retry(session, PEERINGDB_NET_URL, params={"asn": asn}, timeout=20)
        results = resp.json().get("data", [])
    except Exception as e:
        return {"asn": asn, "name": None, "info_type": None, "org_id": None,
                "accepted": False, "score": None, "reason": f"error:{e}"}
    finally:
        polite_sleep(delay)  # always pace, even on failure — don't hammer a struggling API

    if not results:
        # Not found in PeeringDB at all. This is genuine uncertainty, not a confident
        # rejection — a small non-zero score reflects that, distinct from a network
        # explicitly typed as an end-user ISP.
        return {"asn": asn, "name": None, "info_type": None, "org_id": None,
                "accepted": False, "score": 10, "reason": "not_in_peeringdb"}

    net = results[0]
    name = net.get("name", "") or ""
    info_type = net.get("info_type", "") or ""
    org_id = net.get("org_id")
    name_lower = name.lower()

    accept_types = set(cfg["classification"]["accept_info_types"])
    reject_types = set(cfg["classification"]["reject_info_types"])
    keywords = cfg["classification"]["name_keywords_accept"]

    type_match = info_type in accept_types
    keyword_match = any(kw in name_lower for kw in keywords)

    base = {"asn": asn, "name": name, "info_type": info_type, "org_id": org_id}

    if type_match and keyword_match:
        return {**base, "accepted": True, "score": 70, "reason": "peeringdb_type+name_keyword"}
    if type_match:
        return {**base, "accepted": True, "score": 60, "reason": "peeringdb_type"}
    if keyword_match:
        return {**base, "accepted": True, "score": 45, "reason": "name_keyword"}
    if info_type in reject_types:
        return {**base, "accepted": False, "score": 5, "reason": "peeringdb_reject_type"}

    return {**base, "accepted": False, "score": 10, "reason": "default_reject"}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--region", default="all")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--max-asns", type=int, default=None)
    args = parser.parse_args()

    cfg = load_config()
    cache = JSONCache(ROOT / cfg["cache"]["dir"], cfg["cache"]["ttl_days"])
    ckpt = Checkpoint(ROOT / "data" / "checkpoints" / "classify_done.json")

    pairs = load_region_countries(args.region)
    max_asns = args.max_asns or cfg["batch"]["max_asns_per_run"]

    session = requests.Session()
    session.headers.update({"User-Agent": "telecom-core-ranges-bot/1.0"})
    api_key = os.environ.get("PEERINGDB_API_KEY")
    if api_key:
        session.headers.update({"Authorization": f"Api-Key {api_key}"})

    processed = 0
    for region_name, cc in pairs:
        asns = cache.get(f"country_asns_{cc}") or []
        classified_key = f"classified_{cc}"
        classified = cache.get(classified_key) or {}

        for asn in asns:
            ckpt_key = f"cls_{asn}"
            if args.resume and ckpt.is_done(ckpt_key):
                continue
            if processed >= max_asns:
                print(f"[paused] reached the limit of {max_asns} ASNs for this run. Continue later with --resume")
                cache.set(classified_key, classified)
                print(f"stopped early after processing {processed} ASNs.")
                return

            if asn in classified:
                continue

            result = classify_one(session, asn, cfg, cfg["rate_limit"]["peeringdb_delay_seconds"])
            processed += 1

            if result["reason"].startswith("error:"):
                # Transient failure (timeout, connection error, etc.) — don't save this as
                # a real classification decision. Leave it un-checkpointed so it's retried
                # on the next run instead of being locked in as "rejected" for 30 days.
                print(f"[{cc}] AS{asn} -> retry later ({result['reason']})", file=sys.stderr)
                continue

            classified[asn] = result
            # Save immediately, per ASN, so this matches the checkpoint's granularity.
            # If the job is killed mid-country (e.g. by the Actions timeout), nothing
            # already checkpointed is left un-saved.
            cache.set(classified_key, classified)
            ckpt.mark_done(ckpt_key)
            tag = "✓ Core" if result["accepted"] else "✗ excluded"
            print(f"[{cc}] AS{asn} -> {tag} ({result['reason']})")

    print(f"done: classified {processed} new ASNs in this run.")


if __name__ == "__main__":
    main()
