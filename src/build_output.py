"""
Step 4 (final): combine all the classified data stored in the cache into final CSV files.
Makes no network requests — works only on data already stored locally in data/asn_cache.
Also drops any ASN with fewer announced prefixes than config.yml's
classification.min_announced_prefixes (default 1) — a network that barely announces
anything is unlikely to really be core/backbone infrastructure.

Outputs:
    output/all_core_ranges.csv          accepted ASNs only, one row per prefix
                                         (columns include a 0-100 score, not just
                                         accept/reject, plus org_id for grouping ASNs
                                         that belong to the same organisation)
    output/by_region/<region>.csv       same, split by region
    output/classification_report.csv    every evaluated ASN, one row each, accepted
                                         and excluded alike, with its score and reason —
                                         review borderline cases or apply your own
                                         score threshold here instead of the binary cut
"""
import csv
import ipaddress
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from utils.cache import JSONCache
from fetch_asns import load_region_countries, load_config

ROOT = Path(__file__).resolve().parent.parent
FIELDS = ["region", "country", "asn", "org_name", "org_id", "info_type", "score", "prefix"]


def _is_valid_prefix(prefix) -> bool:
    """True if `prefix` parses as a valid IPv4/IPv6 CIDR network."""
    try:
        ipaddress.ip_network(prefix, strict=False)
        return True
    except (ValueError, TypeError):
        return False


def build_rows(all_pairs, cache, min_prefixes=1):
    """
    Turns cached classification + prefix data into output rows.
    Pure aside from reading from `cache`, so it's easy to unit test with a fake cache
    object that just implements .get(key) — see tests/test_project.py.
    Returns (all_rows, by_region_rows, skipped_too_few, skipped_invalid).
    """
    by_region_rows = {}
    all_rows = []
    skipped_too_few = 0
    skipped_invalid = 0

    for region_name, cc in all_pairs:
        classified = cache.get(f"classified_{cc}") or {}
        for asn, info in classified.items():
            if not info.get("accepted"):
                continue
            raw_prefixes = cache.get(f"prefixes_{asn}") or []
            prefixes = []
            for p in raw_prefixes:
                if _is_valid_prefix(p):
                    prefixes.append(p)
                else:
                    skipped_invalid += 1
            if len(prefixes) < min_prefixes:
                skipped_too_few += 1
                continue
            for prefix in prefixes:
                row = {
                    "region": region_name,
                    "country": cc,
                    "asn": asn,
                    "org_name": info.get("name") or "",
                    "org_id": info.get("org_id") or "",
                    "info_type": info.get("info_type") or "",
                    "score": info.get("score") if info.get("score") is not None else "",
                    "prefix": prefix,
                }
                all_rows.append(row)
                by_region_rows.setdefault(region_name, []).append(row)

    return all_rows, by_region_rows, skipped_too_few, skipped_invalid


REPORT_FIELDS = ["region", "country", "asn", "org_name", "org_id", "info_type",
                 "accepted", "score", "prefix_count", "reason", "sources"]


def build_full_report(all_pairs, cache):
    """
    Unlike build_rows (accepted ASNs only, one row per prefix), this covers every ASN
    that was actually evaluated — accepted or not — one row per ASN, so borderline and
    excluded cases stay visible instead of just silently not appearing anywhere. `score`
    is blank (not 0) for ASNs where classify_asn.py never got a usable PeeringDB
    response — those aren't in this cache at all, since a lookup error is intentionally
    left un-checkpointed and retried on the next run rather than being recorded as a
    result (see classify_asn.py). Returns a list of row dicts.
    """
    rows = []
    for region_name, cc in all_pairs:
        classified = cache.get(f"classified_{cc}") or {}
        for asn, info in classified.items():
            prefixes = cache.get(f"prefixes_{asn}") or []
            rows.append({
                "region": region_name,
                "country": cc,
                "asn": asn,
                "org_name": info.get("name") or "",
                "org_id": info.get("org_id") or "",
                "info_type": info.get("info_type") or "",
                "accepted": info.get("accepted", False),
                "score": info.get("score") if info.get("score") is not None else "",
                "prefix_count": len(prefixes),
                "reason": info.get("reason") or "",
                "sources": "PeeringDB,RIPEstat",
            })
    return rows


def main():
    cfg = load_config()
    cache = JSONCache(ROOT / cfg["cache"]["dir"], cfg["cache"]["ttl_days"])
    min_prefixes = cfg["classification"].get("min_announced_prefixes", 1)

    all_pairs = load_region_countries("all")
    all_rows, by_region_rows, skipped_too_few, skipped_invalid = build_rows(all_pairs, cache, min_prefixes)

    if skipped_too_few:
        print(f"[filtered] {skipped_too_few} ASN(s) skipped for announcing fewer than {min_prefixes} prefix(es)")
    if skipped_invalid:
        print(f"[filtered] {skipped_invalid} malformed prefix(es) discarded (failed CIDR validation)")

    out_dir = ROOT / cfg["output"]["by_region_dir"]
    out_dir.mkdir(parents=True, exist_ok=True)

    combined_path = ROOT / cfg["output"]["combined_file"]
    with open(combined_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(all_rows)
    print(f"[saved] {combined_path} ({len(all_rows)} rows)")

    for region_name, rows in by_region_rows.items():
        region_path = out_dir / f"{region_name}.csv"
        with open(region_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=FIELDS)
            writer.writeheader()
            writer.writerows(rows)
        print(f"[saved] {region_path} ({len(rows)} rows)")

    report_rows = build_full_report(all_pairs, cache)
    report_path = ROOT / "output" / "classification_report.csv"
    with open(report_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=REPORT_FIELDS)
        writer.writeheader()
        writer.writerows(report_rows)
    accepted_n = sum(1 for r in report_rows if r["accepted"])
    print(f"[saved] {report_path} ({len(report_rows)} ASNs evaluated, {accepted_n} accepted, "
          f"{len(report_rows) - accepted_n} excluded — use this file to review borderline cases "
          f"or pick your own score threshold)")


if __name__ == "__main__":
    main()
