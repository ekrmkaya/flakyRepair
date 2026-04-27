#!/usr/bin/env python3
"""
Step 5: Final Assembly.

Loads all row{N}_metadata.json files for REPRODUCED rows, validates required
fields, writes flaky_test_data.json and timing_report.json.

Output JSON schema (fields in order, matching existing flaky_test_data.json):
  od_or_id, source, reproduction_steps,
  victim_test_name, polluter_test_name,
  error_messages, failing_lines,
  global_variables, helper_methods, full_test_code

timing_report.json structure:
  {
    "row01": {
      "clone_sec":          12.3,
      "checkout_sec":        0.5,
      "compile_sec":        45.2,
      "victim_alone_sec":    8.1,
      "polluter_victim_sec":12.4,
      "source_locate_sec":   0.1,
      "error_extract_sec":   0.2,
      "total_sec":          78.8
    },
    ...
  }
"""

import json
import os


# ── Required output fields ────────────────────────────────────────────────────

REQUIRED_FIELDS = [
    "od_or_id",
    "source",
    "reproduction_steps",
    "victim_test_name",
    "polluter_test_name",
    "error_messages",
    "failing_lines",
    "global_variables",
    "helper_methods",
    "full_test_code",
]

# Timing fields tracked per pair (from manifest)
TIMING_FIELDS = [
    "clone_sec",
    "checkout_sec",
    "compile_sec",
    "victim_alone_sec",
    "polluter_victim_sec",
    "source_locate_sec",
    "error_extract_sec",
]


def _validate(metadata, row_key):
    """
    Check that all required fields are present and non-empty.
    Prints a warning for any missing/empty field.
    Returns True if the record is usable (required non-empty fields present).
    """
    ok = True
    # Fields that MUST be non-empty for the record to be usable
    critical = {"od_or_id", "source", "victim_test_name", "polluter_test_name",
                "full_test_code", "reproduction_steps"}
    for field in REQUIRED_FIELDS:
        val = metadata.get(field)
        if val is None or val == "" or val == [] or val == {}:
            level = "ERROR" if field in critical else "WARN"
            print(f"  [{level}] {row_key}: field '{field}' is empty")
            if field in critical:
                ok = False
    return ok


def _load_existing_json(out_json):
    """Load existing flaky_test_data.json; return list of records or []."""
    if os.path.isfile(out_json):
        try:
            with open(out_json, encoding="utf-8") as f:
                return json.load(f).get("testdata", [])
        except (json.JSONDecodeError, KeyError):
            pass
    return []


def _load_existing_timing(timing_json):
    """Load existing timing_report.json; return dict or {}."""
    if os.path.isfile(timing_json):
        try:
            with open(timing_json, encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, KeyError):
            pass
    return {}


def run_step5(pairs, manifest_path, output_dir, json_mode="overwrite"):
    """
    Assemble final JSON and timing report from per-row metadata files.
    """
    print("\n" + "=" * 70)
    print("STEP 5: Final Assembly")
    print("=" * 70)

    reproduced = [p for p in pairs if p.get("step3_status") == "REPRODUCED"]
    print(f"{len(reproduced)} REPRODUCED pairs\n")

    testdata = []
    timing   = {}
    skipped  = 0

    for pair in reproduced:
        row_key  = pair["row_key"]
        meta_path = os.path.join(output_dir, f"{row_key}_metadata.json")

        if not os.path.isfile(meta_path):
            print(f"  MISSING metadata: {row_key} — skipping")
            skipped += 1
            continue

        with open(meta_path, encoding="utf-8") as f:
            metadata = json.load(f)

        if not _validate(metadata, row_key):
            print(f"  SKIP {row_key}: failed validation")
            skipped += 1
            continue

        # Build ordered output record
        record = {field: metadata.get(field, "") for field in REQUIRED_FIELDS}
        testdata.append(record)

        # Build timing record from manifest pair
        t = {field: pair.get(field, 0.0) for field in TIMING_FIELDS}
        t["total_sec"] = sum(t.values())
        timing[row_key] = t

    # ── Write outputs ──────────────────────────────────────────────────────────
    out_json    = os.path.join(output_dir, "flaky_test_data.json")
    timing_json = os.path.join(output_dir, "timing_report.json")

    os.makedirs(output_dir, exist_ok=True)

    if json_mode == "overwrite":
        final_testdata = testdata
        final_timing   = timing

    elif json_mode == "append":
        existing = _load_existing_json(out_json)
        existing_names = {r["victim_test_name"] for r in existing}
        added = [r for r in testdata if r["victim_test_name"] not in existing_names]
        final_testdata = existing + added
        final_timing = {**_load_existing_timing(timing_json), **timing}
        print(f"  (append mode: {len(added)} new, {len(testdata)-len(added)} already present)")

    elif json_mode == "replace-rows":
        existing = _load_existing_json(out_json)
        new_by_name = {r["victim_test_name"]: r for r in testdata}
        replaced, added = 0, 0
        final_testdata = []
        for r in existing:
            if r["victim_test_name"] in new_by_name:
                final_testdata.append(new_by_name.pop(r["victim_test_name"]))
                replaced += 1
            else:
                final_testdata.append(r)
        for r in new_by_name.values():   # new rows not in existing
            final_testdata.append(r)
            added += 1
        final_timing = {**_load_existing_timing(timing_json), **timing}
        print(f"  (replace-rows mode: {replaced} replaced, {added} new)")

    else:
        raise ValueError(f"Unknown json_mode: {json_mode!r}")

    with open(out_json, "w", encoding="utf-8") as f:
        json.dump({"testdata": final_testdata}, f, indent=2)
    print(f"\nWrote {len(final_testdata)} records to {out_json}")

    with open(timing_json, "w", encoding="utf-8") as f:
        json.dump(final_timing, f, indent=2)
    print(f"Wrote timing report to {timing_json}")

    # ── Summary ────────────────────────────────────────────────────────────────
    counts = {}
    for p in pairs:
        s = (p.get("step3_status") or
             p.get("step2_status") or
             p.get("step1_status", "?"))
        counts[s] = counts.get(s, 0) + 1

    if skipped:
        print(f"  ({skipped} REPRODUCED rows skipped due to missing/invalid metadata)")

    print(f"\nFINAL COUNTS: {counts}")

    if timing:
        total_secs = [t["total_sec"] for t in timing.values()]
        avg = sum(total_secs) / len(total_secs)
        print(f"\nTIMING (wall clock per row):")
        print(f"  Rows timed   : {len(total_secs)}")
        print(f"  Average total: {avg:.1f}s")
        for field in TIMING_FIELDS:
            vals = [t.get(field, 0.0) for t in timing.values()]
            if any(v > 0 for v in vals):
                print(f"  Avg {field:<24}: {sum(vals)/len(vals):.1f}s")

    return testdata


# ── Standalone runner ──────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse

    HERE = os.path.dirname(os.path.abspath(__file__))
    _out = os.path.join(HERE, "output")

    ap = argparse.ArgumentParser()
    ap.add_argument("--output-dir", default=_out)
    args = ap.parse_args()

    manifest_path = os.path.join(args.output_dir, "manifest.json")

    with open(manifest_path) as f:
        manifest = json.load(f)
    pairs = manifest["pairs"]

    run_step5(pairs, manifest_path, args.output_dir)
