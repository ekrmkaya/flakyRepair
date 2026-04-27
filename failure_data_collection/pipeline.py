#!/usr/bin/env python3
"""
pipeline.py — Orchestrator for the failure data collection pipeline.

Runs steps 1–5 in sequence for the specified rows of flaky_tests.csv.

Usage:
  python pipeline.py                         # all rows in CSV
  python pipeline.py --rows 1 2 3            # specific rows
  python pipeline.py --rows 1-10             # range notation
  python pipeline.py --input /path/to.csv --output-dir /path/to/out

Steps:
  1. step1_setup.py       — Clone, checkout, Java detection
  2. step2_prebuild.py    — Pre-compile all repos (batch, before repro)
  3. step3_reproduce.py   — Two-phase reproduction confirmation
  4. step4_extract.py     — Code + error extraction
  5. step5_assemble.py    — Final JSON assembly + timing report

The manifest (output/manifest.json) is written after each step so partial
runs are resumable. Re-running with the same rows will detect existing
results and prompt before overwriting.
"""

import argparse
import json
import os
import sys
import time

# ── Path setup ─────────────────────────────────────────────────────────────────
HERE     = os.path.dirname(os.path.abspath(__file__))
BASE_DIR = os.path.dirname(os.path.dirname(HERE))  # …/dataCollection/

sys.path.insert(0, HERE)

from step1_setup   import run_step1
from step2_prebuild import run_step2
from step3_reproduce import run_step3
from step4_extract  import run_step4
from step5_assemble import run_step5


# ── Row argument parsing ───────────────────────────────────────────────────────

def _parse_row_args(row_args):
    """
    Parse row specifiers like ['1', '2', '3-5', '10'] into a sorted list of
    1-based integers.
    """
    rows = set()
    for arg in row_args:
        if "-" in arg:
            lo, hi = arg.split("-", 1)
            rows.update(range(int(lo), int(hi) + 1))
        else:
            rows.add(int(arg))
    return sorted(rows)


# ── Overwrite detection ────────────────────────────────────────────────────────

def _has_existing_results(pairs, target_rows, output_dir):
    """
    Return True if any of the target rows already have a terminal step3_status
    or a metadata file in output_dir.
    """
    target_keys = {f"row{r:02d}" for r in target_rows} if target_rows else None

    for pair in pairs:
        if target_keys and pair.get("row_key") not in target_keys:
            continue
        if pair.get("step3_status") is not None:
            return True
        meta = os.path.join(output_dir, f"{pair['row_key']}_metadata.json")
        if os.path.isfile(meta):
            return True
    return False


def _prompt_overwrite():
    try:
        ans = input("Existing results detected. Overwrite? [y/N] ").strip().lower()
        return ans in ("y", "yes")
    except (EOFError, KeyboardInterrupt):
        return False


def _clear_row_results(pair, output_dir):
    """Reset step3+ fields so the row is re-processed from step3 onwards."""
    for field in ["step3_status", "strategy_used",
                  "victim_alone_sec", "polluter_victim_sec",
                  "phase_a_log", "phase_b_log",
                  "source_locate_sec", "error_extract_sec"]:
        pair.pop(field, None)
    meta = os.path.join(output_dir, f"{pair['row_key']}_metadata.json")
    if os.path.isfile(meta):
        os.remove(meta)


# ── Timing helper ──────────────────────────────────────────────────────────────

class TimingCollector:
    def __init__(self):
        self._start = time.time()
        self.wall_sec = 0.0

    def stop(self):
        self.wall_sec = time.time() - self._start


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description="Failure data collection pipeline.")
    ap.add_argument(
        "--rows", nargs="*", metavar="N",
        help="Row numbers to process (e.g. 1 2 3-5). Default: all rows.",
    )
    ap.add_argument(
        "--input", default=None,
        help="Path to flaky_tests.csv. Default: <repo_root>/flaky_tests.csv",
    )
    ap.add_argument(
        "--output-dir", default=None,
        help="Output directory. Default: <this_dir>/output/",
    )
    ap.add_argument(
        "--skip-step1", action="store_true",
        help="Skip step1 (clone/checkout). Requires existing manifest.",
    )
    ap.add_argument(
        "--skip-step2", action="store_true",
        help="Skip step2 (pre-build). Useful if repos are already compiled.",
    )
    ap.add_argument(
        "--force", action="store_true",
        help="Overwrite existing results without prompting.",
    )
    ap.add_argument(
        "--json-mode",
        choices=["overwrite", "append", "replace-rows"],
        default="overwrite",
        help=(
            "How to update flaky_test_data.json. "
            "'overwrite': replace entire file (default). "
            "'append': add only rows not already present (matched by victim_test_name). "
            "'replace-rows': update matching rows in-place, add new ones, keep the rest."
        ),
    )
    args = ap.parse_args()

    # Defaults
    csv_path   = args.input   or os.path.join(BASE_DIR, "openai/data/final_OD_flaky_tests.csv")
    output_dir = args.output_dir or os.path.join(HERE, "output")
    manifest_path = os.path.join(output_dir, "manifest.json")
    logs_dir      = os.path.join(output_dir, "logs")
    repos_dir     = os.path.join(BASE_DIR, "repos")

    os.makedirs(output_dir, exist_ok=True)
    os.makedirs(logs_dir, exist_ok=True)

    target_rows = _parse_row_args(args.rows) if args.rows else None

    print("=" * 70)
    print("FAILURE DATA COLLECTION PIPELINE")
    print("=" * 70)
    print(f"  CSV      : {csv_path}")
    print(f"  Output   : {output_dir}")
    print(f"  Repos    : {repos_dir}")
    if target_rows:
        print(f"  Rows     : {target_rows}")
    else:
        print(f"  Rows     : ALL")
    print()

    # ── Step 1: Clone + checkout ───────────────────────────────────────────────
    if not args.skip_step1:
        timer = TimingCollector()
        pairs = run_step1(
            csv_path      = csv_path,
            repos_dir     = repos_dir,
            manifest_path = manifest_path,
            logs_dir      = logs_dir,
            target_rows   = target_rows,
        )
        timer.stop()
    else:
        if not os.path.isfile(manifest_path):
            sys.exit(f"ERROR: --skip-step1 requires existing manifest at {manifest_path}")
        with open(manifest_path) as f:
            all_pairs = json.load(f)["pairs"]
        print("Step 1 skipped — loaded existing manifest.")
        pairs = all_pairs

    # Filter to target rows after step1 (step1 already filtered; handle skip case).
    # Keep full_pairs so non-target rows are preserved in manifest writes.
    full_pairs = pairs  # reference to all rows from manifest/step1
    if target_rows:
        pairs = [p for p in pairs if p.get("row_num") in target_rows]

    # Overwrite detection — check after step1 since pairs are now populated
    if os.path.isfile(manifest_path) and _has_existing_results(pairs, target_rows, output_dir):
        if not args.force and not _prompt_overwrite():
            print("Aborting. Existing results preserved.")
            sys.exit(0)
        # Clear step3+ results so they re-run
        for pair in pairs:
            if target_rows is None or pair.get("row_num") in target_rows:
                _clear_row_results(pair, output_dir)

    # ── Step 2: Pre-build ──────────────────────────────────────────────────────
    if not args.skip_step2:
        pairs = run_step2(pairs, manifest_path, logs_dir)
    else:
        print("Step 2 skipped — assuming all repos already compiled.")
        for p in pairs:
            if p.get("step1_status") == "READY" and not p.get("step2_status"):
                p["step2_status"] = "BUILD_OK"

    # ── Step 3: Reproduction ───────────────────────────────────────────────────
    # Pass full_pairs so manifest writes preserve all rows, not just the subset.
    pairs = run_step3(pairs, manifest_path, logs_dir,
                      all_pairs=full_pairs if target_rows else None)

    # ── Step 4: Extraction ─────────────────────────────────────────────────────
    # Pass full_pairs so manifest writes preserve all rows, not just the subset.
    pairs = run_step4(pairs, manifest_path, output_dir,
                      all_pairs=full_pairs if target_rows else None)

    # Merge processed pairs back into the full list so manifest and step5 see
    # all rows (not just the ones in this partial run).
    if target_rows:
        row_by_num = {p.get("row_num"): p for p in pairs}
        merged = []
        for p in full_pairs:
            merged.append(row_by_num.get(p.get("row_num"), p))
        pairs = merged

    # ── Step 5: Assembly ───────────────────────────────────────────────────────
    run_step5(pairs, manifest_path, output_dir, json_mode=args.json_mode)

    print("\n" + "=" * 70)
    print("PIPELINE COMPLETE")
    print(f"  Results : {os.path.join(output_dir, 'flaky_test_data.json')}")
    print(f"  Timing  : {os.path.join(output_dir, 'timing_report.json')}")
    print("=" * 70)


if __name__ == "__main__":
    main()
