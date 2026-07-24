#!/usr/bin/env python3
"""
Convenience wrapper: runs all 4 pipeline steps in order with the same --region/--resume
flags, stopping immediately if any step fails. Equivalent to running fetch_asns.py,
classify_asn.py, fetch_prefixes.py, and build_output.py one after another by hand —
handy for a single command when running locally (e.g. from Termux on Android) instead
of four separate ones.

GitHub Actions still runs each step as its own workflow step rather than calling this
script, so the Actions UI keeps clear separate timing/logs per stage — this is purely a
convenience for humans running the pipeline manually.

Usage:
    python run_pipeline.py --region southeast_asia --resume
    python run_pipeline.py --region all --resume
"""
import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
FETCH_STEPS = ["src/fetch_asns.py", "src/classify_asn.py", "src/fetch_prefixes.py"]
BUILD_STEP = "src/build_output.py"


def run_step(step: str, region: str, resume: bool) -> int:
    cmd = [sys.executable, str(ROOT / step), "--region", region]
    if resume:
        cmd.append("--resume")
    print(f"\n=== {step} ===")
    return subprocess.run(cmd).returncode


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--region", default="all")
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args()

    for step in FETCH_STEPS:
        code = run_step(step, args.region, args.resume)
        if code != 0:
            print(f"\n[stopped] {step} exited with code {code} — fix that before continuing", file=sys.stderr)
            sys.exit(code)

    print(f"\n=== {BUILD_STEP} ===")
    sys.exit(subprocess.run([sys.executable, str(ROOT / BUILD_STEP)]).returncode)


if __name__ == "__main__":
    main()
