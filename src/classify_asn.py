"""
Step 2: classify each fetched ASN as Core/Backbone or User-facing, with a 0-100
confidence score (not just a binary decision).
Source: PeeringDB (no key needed for public reads, with a reasonable request limit).
Output: data/asn_cache/classified_<CC>.json
        { asn: {name, info_type, org_id, accepted: bool, score: int|None, reason} }

Queries PeeringDB in batches of up to `peeringdb_batch_size` ASNs per request (via the
asn__in filter), per PeeringDB's own guidance at
docs.peeringdb.com/howto/work_within_peeringdbs_query_limits — sending one request per
ASN is exactly what that guidance says not to do, and running that way is what
triggered PeeringDB's per-IP rate limit (20 req/min unauthenticated, 40/min with an
API key) in practice.

Usage:
    python src/classify_asn.py --region southeast_asia --resume
"""
import argparse
import os
import re
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
DEFAULT_BATCH_SIZE = 150  # PeeringDB's documented per-query maximum


def _score_net(asn: str, net: dict, cfg) -> dict:
    """
    Pure scoring logic shared by classify_one and classify_batch: given a PeeringDB
    net record (or None if nothing matched this ASN), returns the classification dict.
    No network access, so this is the easiest place to unit test the scoring rubric.
    """
    if net is None:
        # Not found in PeeringDB at all. This is genuine uncertainty, not a confident
        # rejection — a small non-zero score reflects that, distinct from a network
        # explicitly typed as an end-user ISP.
        return {"asn": asn, "name": None, "info_type": None, "org_id": None,
                "accepted": False, "score": 10, "reason": "not_in_peeringdb"}

    name = net.get("name", "") or ""
    info_type = net.get("info_type", "") or ""
    org_id = net.get("org_id")
    name_lower = name.lower()

    accept_types = set(cfg["classification"]["accept_info_types"])
    reject_types = set(cfg["classification"]["reject_info_types"])
    keywords = cfg["classification"]["name_keywords_accept"]

    type_match = info_type in accept_types
    # Whole-word match only — a plain substring check was matching "ix" inside
    # "MATRONIX"/"TPIX"/"CePIX" and "core" inside "NCORE", overriding PeeringDB's own
    # Cable/DSL/ISP classification for networks that are, per PeeringDB itself, not
    # core/backbone. A hyphen or space still counts as a boundary (still matches
    # "KG-IX", "SNS-IX"), just not a mid-word coincidence.
    keyword_match = any(re.search(r"\b" + re.escape(kw) + r"\b", name_lower) for kw in keywords)

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


def classify_one(session, asn: str, cfg, delay: float):
    """
    Queries PeeringDB for a single ASN. Kept for convenience (e.g. re-checking one
    ASN by hand) — classify_batch() below is what the pipeline actually uses, since
    querying one ASN per request is what triggers PeeringDB's rate limit. A single-ASN
    query 404s (not 200-with-empty-data) when nothing matches, unlike the batch
    endpoint — that's handled here explicitly.
    """
    try:
        resp = get_with_retry(session, PEERINGDB_NET_URL, params={"asn": asn}, timeout=20)
        results = resp.json().get("data", [])
    except requests.HTTPError as e:
        if e.response is not None and e.response.status_code == 404:
            return _score_net(asn, None, cfg)
        return {"asn": asn, "name": None, "info_type": None, "org_id": None,
                "accepted": False, "score": None, "reason": f"error:{e}"}
    except Exception as e:
        return {"asn": asn, "name": None, "info_type": None, "org_id": None,
                "accepted": False, "score": None, "reason": f"error:{e}"}
    finally:
        polite_sleep(delay)  # always pace, even on failure — don't hammer a struggling API

    return _score_net(asn, results[0] if results else None, cfg)


def classify_batch(session, asn_batch, cfg, delay: float) -> dict:
    """
    Classifies up to len(asn_batch) ASNs (should be <= DEFAULT_BATCH_SIZE) in a single
    PeeringDB request using asn__in. Returns {asn: result_dict} for every ASN in the
    batch. A batch query returns 200 with whatever subset matched — no 404 case to
    handle, unlike single-ASN lookups. If the whole request fails, every ASN in the
    batch gets the same error result (all retried together on the next --resume run).
    """
    asn_list_str = ",".join(str(a) for a in asn_batch)
    try:
        resp = get_with_retry(session, PEERINGDB_NET_URL, params={"asn__in": asn_list_str}, timeout=30)
        results = resp.json().get("data", [])
    except Exception as e:
        return {asn: {"asn": asn, "name": None, "info_type": None, "org_id": None,
                       "accepted": False, "score": None, "reason": f"error:{e}"}
                for asn in asn_batch}
    finally:
        polite_sleep(delay)  # always pace, even on failure — don't hammer a struggling API

    by_asn = {str(net.get("asn")): net for net in results if net.get("asn") is not None}
    return {asn: _score_net(asn, by_asn.get(str(asn)), cfg) for asn in asn_batch}


def _chunks(items, size):
    for i in range(0, len(items), size):
        yield items[i:i + size]


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
    batch_size = cfg["rate_limit"].get("peeringdb_batch_size", DEFAULT_BATCH_SIZE)

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

        todo = [a for a in asns
                if a not in classified and not (args.resume and ckpt.is_done(f"cls_{a}"))]

        # A malformed value (e.g. corrupted legacy cache data) will never succeed no
        # matter how many times it's retried — skip it and checkpoint it immediately
        # instead of sending it to PeeringDB and looping on it forever.
        valid_todo = []
        for a in todo:
            if re.fullmatch(r"\d+", str(a)):
                valid_todo.append(a)
            else:
                print(f"[{cc}] skipping malformed ASN value {a!r} — not retryable, marking done", file=sys.stderr)
                classified[a] = {"asn": a, "name": None, "info_type": None, "org_id": None,
                                  "accepted": False, "score": None, "reason": "invalid_asn_value"}
                ckpt.mark_done(f"cls_{a}")
        if valid_todo != todo:
            cache.set(classified_key, classified)

        for batch in _chunks(valid_todo, batch_size):
            if processed >= max_asns:
                print(f"[paused] reached the limit of {max_asns} ASNs for this run. Continue later with --resume")
                print(f"stopped early after processing {processed} ASNs.")
                return

            # Don't let a batch push us past max_asns — trim it instead of overshooting.
            remaining = max_asns - processed
            if len(batch) > remaining:
                batch = batch[:remaining]

            results = classify_batch(session, batch, cfg, cfg["rate_limit"]["peeringdb_delay_seconds"])
            processed += len(batch)

            for asn, result in results.items():
                if result["reason"].startswith("error:"):
                    # Whole-batch failure — don't checkpoint, so it's retried next run
                    # instead of being locked in as "rejected" for 30 days.
                    print(f"[{cc}] AS{asn} -> retry later ({result['reason']})", file=sys.stderr)
                    continue
                classified[asn] = result
                ckpt.mark_done(f"cls_{asn}")
                tag = "✓ Core" if result["accepted"] else "✗ excluded"
                print(f"[{cc}] AS{asn} -> {tag} ({result['reason']})")

            # Save once per batch, not once per country — a batch is now the atomic
            # unit of work (matches the checkpoint granularity: everything just marked
            # done above is saved here before moving to the next batch).
            cache.set(classified_key, classified)

    print(f"done: classified {processed} new ASNs in this run.")


if __name__ == "__main__":
    main()
