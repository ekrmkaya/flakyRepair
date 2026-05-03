#!/usr/bin/env python3
"""
Section 7: Metrics Assembly & CSV Output

Joins section 1–6 data into two CSVs:

1. results.csv  (one row per test_id × condition)
   Columns: metadata + outcome + full timing/token breakdown

2. summary.csv  (one row per test_id, both conditions side-by-side)
   Columns: metadata + [with_repro] outcome/tokens/time + [no_repro] outcome/tokens/time

Timing breakdown (all in seconds):
  s1_api_sec               — initial GPT-4 patch generation
  s4_or_s5a1_compile_sec   — section 4 / attempt-1 compile
  s4_or_s5a1_stitch_api_sec
  s4_or_s5a1_stitch_compile_sec
  s5_retry_api_sec         — sum of GPT-4 API time for retry attempts (2+)
  s5_retry_compile_sec
  s5_retry_stitch_api_sec
  s5_retry_stitch_compile_sec
  s5_test_sec              — winning OD test run
  s5_victim_alone_sec
  s5_polluter_alone_sec
  total_api_sec            — sum of all GPT-4 API calls
  total_compile_sec        — sum of all compile invocations
  total_elapsed_sec        — total_api + total_compile + test + isolation checks

Token breakdown:
  s1_prompt_tok / s1_completion_tok
  s5_a1_stitch_prompt_tok / s5_a1_stitch_completion_tok  (attempt 1 stitch = s4 stitch)
  s5_retry_prompt_tok / s5_retry_completion_tok           (attempts 2+ GPT-4)
  s5_retry_stitch_prompt_tok / s5_retry_stitch_completion_tok
  total_prompt_tok / total_completion_tok / total_tokens
"""

import csv
import json
import os
import subprocess
import sys

BASE_DIR     = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # repository root
OAI_DIR      = os.path.dirname(os.path.abspath(__file__))
OUT_DIR      = os.path.join(OAI_DIR, "section7_results")

CONDITIONS = {
    "with_repro": {
        "s1_dir": os.path.join(OAI_DIR, "section1_patches"),
        "s5_dir": os.path.join(OAI_DIR, "section5_test_runs"),
        "s6_dir": os.path.join(OAI_DIR, "section6_categories"),
    },
    "no_repro": {
        "s1_dir": os.path.join(OAI_DIR, "section1_patches_no_repro"),
        "s5_dir": os.path.join(OAI_DIR, "section5_test_runs_no_repro"),
        "s6_dir": os.path.join(OAI_DIR, "section6_categories"),
    },
}


# ── helpers ───────────────────────────────────────────────────────────────────

def read_file(path):
    try:
        subprocess.run(["xattr", "-c", path], capture_output=True, timeout=1)
    except Exception:
        pass
    try:
        r = subprocess.run(["cat", path], capture_output=True, text=True, timeout=10)
        if r.returncode == 0 and r.stdout:
            return r.stdout
    except Exception:
        pass
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except Exception:
        return ""


def rfile(dirpath, name):
    p = os.path.join(dirpath, name)
    if not os.path.isfile(p):
        return ""
    return read_file(p).strip()


def load_json(path):
    if not os.path.isfile(path):
        return {}
    try:
        return json.loads(read_file(path))
    except Exception:
        return {}


def _sum(*vals):
    """Sum numeric values, treating None as 0."""
    return sum(v for v in vals if v is not None)


def _null_sum(vals):
    """Sum a list, returning None if all are None, else numeric sum."""
    numeric = [v for v in vals if v is not None]
    return sum(numeric) if numeric else None


# ── aggregate section-5 attempt metrics ──────────────────────────────────────

def aggregate_attempt_metrics(attempt_list):
    """
    Splits attempt_metrics into:
      - attempt 1 (source = section4 / section4_initial / section4_stitched):
        compile, optional stitch — these are the section 4 costs
      - attempts 2+ (source = gpt4_retry):
        GPT-4 retry cost + compile + optional stitch

    Returns a flat dict of aggregated values.
    """
    a = {
        # attempt 1 (= section 4 work)
        "a1_compile_sec":            None,
        "a1_stitch_api_sec":         None,
        "a1_stitch_compile_sec":     None,
        "a1_stitch_prompt_tok":      None,
        "a1_stitch_completion_tok":  None,
        # retries (attempts 2+)
        "retry_api_sec":             None,
        "retry_prompt_tok":          None,
        "retry_completion_tok":      None,
        "retry_stitch_api_sec":      None,
        "retry_stitch_compile_sec":  None,
        "retry_stitch_prompt_tok":   None,
        "retry_stitch_completion_tok": None,
        "retry_compile_sec":         None,
        # test/isolation from winning attempt
        "test_sec":                  None,
        "victim_alone_sec":          None,
        "polluter_alone_sec":        None,
        # attempt count
        "attempts_used":             len(attempt_list),
        "stitches_used":             0,
        "stitch_succeeded":          0,
        "early_exit":                False,
    }

    retry_api_secs    = []
    retry_prompt_toks = []
    retry_comp_toks   = []
    retry_stitch_api  = []
    retry_stitch_comp = []
    retry_stitch_pt   = []
    retry_stitch_ct   = []
    retry_compile     = []

    for m in attempt_list:
        num = m.get("attempt_num", 0)
        if m.get("stitched"):
            a["stitches_used"] += 1
        if m.get("stitch_worked"):
            a["stitch_succeeded"] += 1

        if num == 1:
            a["a1_compile_sec"]           = m.get("compile_elapsed_seconds")
            a["a1_stitch_api_sec"]        = m.get("stitch_api_elapsed_seconds")
            a["a1_stitch_compile_sec"]    = m.get("stitch_compile_elapsed_seconds")
            a["a1_stitch_prompt_tok"]     = m.get("stitch_prompt_tokens")
            a["a1_stitch_completion_tok"] = m.get("stitch_completion_tokens")
        else:
            if m.get("gpt4_api_elapsed_seconds") is not None:
                retry_api_secs.append(m["gpt4_api_elapsed_seconds"])
            if m.get("gpt4_prompt_tokens") is not None:
                retry_prompt_toks.append(m["gpt4_prompt_tokens"])
            if m.get("gpt4_completion_tokens") is not None:
                retry_comp_toks.append(m["gpt4_completion_tokens"])
            if m.get("stitch_api_elapsed_seconds") is not None:
                retry_stitch_api.append(m["stitch_api_elapsed_seconds"])
            if m.get("stitch_compile_elapsed_seconds") is not None:
                retry_stitch_comp.append(m["stitch_compile_elapsed_seconds"])
            if m.get("stitch_prompt_tokens") is not None:
                retry_stitch_pt.append(m["stitch_prompt_tokens"])
            if m.get("stitch_completion_tokens") is not None:
                retry_stitch_ct.append(m["stitch_completion_tokens"])
            if m.get("compile_elapsed_seconds") is not None:
                retry_compile.append(m["compile_elapsed_seconds"])

        # Winning attempt test/isolation times
        if m.get("attempt_result") == "FIXED":
            a["test_sec"]         = m.get("test_elapsed_seconds")
            a["victim_alone_sec"] = m.get("victim_alone_elapsed_seconds")
            a["polluter_alone_sec"] = m.get("polluter_alone_elapsed_seconds")

    a["retry_api_sec"]             = _null_sum(retry_api_secs)
    a["retry_prompt_tok"]          = _null_sum(retry_prompt_toks)
    a["retry_completion_tok"]      = _null_sum(retry_comp_toks)
    a["retry_stitch_api_sec"]      = _null_sum(retry_stitch_api)
    a["retry_stitch_compile_sec"]  = _null_sum(retry_stitch_comp)
    a["retry_stitch_prompt_tok"]   = _null_sum(retry_stitch_pt)
    a["retry_stitch_completion_tok"] = _null_sum(retry_stitch_ct)
    a["retry_compile_sec"]         = _null_sum(retry_compile)

    return a


# ── collect one row × condition ───────────────────────────────────────────────

def collect_row(row_key, condition, dirs, target):
    s1_metrics = load_json(os.path.join(dirs["s1_dir"], "metrics.json"))
    s1 = s1_metrics.get(row_key, {})

    s5_row_dir = os.path.join(dirs["s5_dir"], row_key)
    # Support both new (row_result.json) and old (separate files) formats
    row_result_path = os.path.join(s5_row_dir, "row_result.json")
    if os.path.isfile(row_result_path):
        row_result = load_json(row_result_path)
        final_status = row_result.get("final_status", "MISSING")
        attempt_list = row_result.get("attempt_metrics", [])
    else:
        attempt_metrics_path = os.path.join(s5_row_dir, "attempt_metrics.json")
        attempt_list = load_json(attempt_metrics_path) if os.path.isfile(attempt_metrics_path) else []
        if not isinstance(attempt_list, list):
            attempt_list = []
        final_status = rfile(s5_row_dir, "final_status.txt") or "MISSING"

    s6_key = f"{row_key}_{condition}"
    s6_dir = os.path.join(dirs["s6_dir"], s6_key)
    category = rfile(s6_dir, "category.txt") or "MISSING"
    passed   = rfile(s6_dir, "passed.txt")   or "N"

    a = aggregate_attempt_metrics(attempt_list)

    # Tokens
    s1_prompt  = s1.get("prompt_tokens")
    s1_comp    = s1.get("completion_tokens")
    s1_total   = s1.get("total_tokens")

    total_prompt_tok = _null_sum([
        s1_prompt,
        a["a1_stitch_prompt_tok"],
        a["retry_prompt_tok"],
        a["retry_stitch_prompt_tok"],
    ])
    total_comp_tok = _null_sum([
        s1_comp,
        a["a1_stitch_completion_tok"],
        a["retry_completion_tok"],
        a["retry_stitch_completion_tok"],
    ])
    total_tokens = (
        (total_prompt_tok or 0) + (total_comp_tok or 0)
        if total_prompt_tok is not None or total_comp_tok is not None
        else None
    )

    # Time
    s1_api_sec = s1.get("elapsed_seconds")
    total_api_sec = _null_sum([
        s1_api_sec,
        a["a1_stitch_api_sec"],
        a["retry_api_sec"],
        a["retry_stitch_api_sec"],
    ])
    total_compile_sec = _null_sum([
        a["a1_compile_sec"],
        a["a1_stitch_compile_sec"],
        a["retry_compile_sec"],
        a["retry_stitch_compile_sec"],
    ])
    total_elapsed_sec = _null_sum([
        total_api_sec,
        total_compile_sec,
        a["test_sec"],
        a["victim_alone_sec"],
        a["polluter_alone_sec"],
    ])

    return {
        # identity
        "test_id":    target["test_id"],
        "row_num":    target["row_num"],
        "condition":  condition,
        "victim":     target.get("victim", target.get("victim_method", "")),
        "polluter":   target.get("polluter", target.get("polluter_method", "")),
        "repo_url":   target.get("repo_url", ""),
        "commit":     target.get("commit", ""),
        # outcome
        "final_status":   final_status,
        "category":       category,
        "passed":         passed,
        "attempts_used":  a["attempts_used"],
        "stitches_used":  a["stitches_used"],
        "stitch_succeeded": a["stitch_succeeded"],
        # section 1
        "s1_prompt_tok":      s1_prompt,
        "s1_completion_tok":  s1_comp,
        "s1_total_tok":       s1_total,
        "s1_api_sec":         s1_api_sec,
        # attempt 1 (= section 4 work)
        "a1_compile_sec":           a["a1_compile_sec"],
        "a1_stitch_prompt_tok":     a["a1_stitch_prompt_tok"],
        "a1_stitch_completion_tok": a["a1_stitch_completion_tok"],
        "a1_stitch_api_sec":        a["a1_stitch_api_sec"],
        "a1_stitch_compile_sec":    a["a1_stitch_compile_sec"],
        # retries (attempts 2+)
        "retry_prompt_tok":           a["retry_prompt_tok"],
        "retry_completion_tok":       a["retry_completion_tok"],
        "retry_api_sec":              a["retry_api_sec"],
        "retry_compile_sec":          a["retry_compile_sec"],
        "retry_stitch_prompt_tok":    a["retry_stitch_prompt_tok"],
        "retry_stitch_completion_tok": a["retry_stitch_completion_tok"],
        "retry_stitch_api_sec":       a["retry_stitch_api_sec"],
        "retry_stitch_compile_sec":   a["retry_stitch_compile_sec"],
        # test/isolation
        "test_sec":           a["test_sec"],
        "victim_alone_sec":   a["victim_alone_sec"],
        "polluter_alone_sec": a["polluter_alone_sec"],
        # totals
        "total_prompt_tok":   total_prompt_tok,
        "total_completion_tok": total_comp_tok,
        "total_tokens":       total_tokens,
        "total_api_sec":      total_api_sec,
        "total_compile_sec":  total_compile_sec,
        "total_elapsed_sec":  total_elapsed_sec,
    }


# ── CSV writers ───────────────────────────────────────────────────────────────

RESULTS_FIELDS = [
    "test_id", "row_num", "condition", "victim", "polluter", "repo_url", "commit",
    "final_status", "category", "passed", "attempts_used", "stitches_used", "stitch_succeeded",
    "s1_prompt_tok", "s1_completion_tok", "s1_total_tok", "s1_api_sec",
    "a1_compile_sec",
    "a1_stitch_prompt_tok", "a1_stitch_completion_tok",
    "a1_stitch_api_sec", "a1_stitch_compile_sec",
    "retry_prompt_tok", "retry_completion_tok", "retry_api_sec", "retry_compile_sec",
    "retry_stitch_prompt_tok", "retry_stitch_completion_tok",
    "retry_stitch_api_sec", "retry_stitch_compile_sec",
    "test_sec", "victim_alone_sec", "polluter_alone_sec",
    "total_prompt_tok", "total_completion_tok", "total_tokens",
    "total_api_sec", "total_compile_sec", "total_elapsed_sec",
]


def write_results_csv(all_rows, path):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=RESULTS_FIELDS, extrasaction="ignore")
        w.writeheader()
        for row in sorted(all_rows, key=lambda r: (r["row_num"], r["condition"])):
            w.writerow(row)


def write_summary_csv(all_rows, path):
    """One row per test_id, with_repro and no_repro columns side-by-side."""
    by_row = {}
    for r in all_rows:
        by_row.setdefault(r["row_num"], {})[r["condition"]] = r

    # Build header: metadata fields + prefixed metrics for each condition
    METRIC_FIELDS = [f for f in RESULTS_FIELDS
                     if f not in ("test_id","row_num","condition","victim","polluter","repo_url","commit")]

    fieldnames = ["test_id","row_num","victim","polluter","repo_url","commit"]
    for cond in ("with_repro", "no_repro"):
        for f in METRIC_FIELDS:
            fieldnames.append(f"{cond}__{f}")

    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for row_num in sorted(by_row.keys()):
            cond_data = by_row[row_num]
            # Use whichever condition is available for metadata
            ref = cond_data.get("with_repro") or cond_data.get("no_repro")
            out = {
                "test_id":   ref["test_id"],
                "row_num":   ref["row_num"],
                "victim":    ref["victim"],
                "polluter":  ref["polluter"],
                "repo_url":  ref["repo_url"],
                "commit":    ref["commit"],
            }
            for cond in ("with_repro", "no_repro"):
                d = cond_data.get(cond, {})
                for mf in METRIC_FIELDS:
                    out[f"{cond}__{mf}"] = d.get(mf, "")
            w.writerow(out)


# ── print summary table ───────────────────────────────────────────────────────

def print_summary(all_rows):
    print("\n" + "=" * 85)
    print(f"{'test_id':<22} {'cond':12} {'status':14} {'cat':28} {'tok':>7} {'elapsed':>8}")
    print("-" * 85)
    for r in sorted(all_rows, key=lambda x: (x["row_num"], x["condition"])):
        cat_short = r["category"][:27]
        tok  = r["total_tokens"]  if r["total_tokens"]  is not None else "-"
        elap = f"{r['total_elapsed_sec']:.1f}s" if r["total_elapsed_sec"] is not None else "-"
        print(f"  {r['test_id']:<20} {r['condition']:<12} {r['final_status']:<14} "
              f"{cat_short:<28} {str(tok):>7} {elap:>8}")

    print("=" * 85)
    for cond in ("with_repro", "no_repro"):
        rows = [r for r in all_rows if r["condition"] == cond]
        if not rows:
            continue
        fixed = sum(1 for r in rows if r["final_status"] == "FIXED")
        ce    = sum(1 for r in rows if r["final_status"] == "COMPILE_ERROR")
        nf    = sum(1 for r in rows if r["final_status"] == "NOT_FIXED")
        total_tok  = sum(r["total_tokens"]      or 0 for r in rows)
        total_elap = sum(r["total_elapsed_sec"] or 0 for r in rows)
        print(f"\n[{cond}]  n={len(rows)}  FIXED={fixed}  COMPILE_ERROR={ce}  NOT_FIXED={nf}"
              f"  total_tokens={total_tok}  total_elapsed={total_elap:.1f}s")


# ── main ─────────────────────────────────────────────────────────────────────

def main():
    print("=" * 60)
    print("SECTION 7: METRICS ASSEMBLY & CSV OUTPUT")
    print("=" * 60)

    os.makedirs(OUT_DIR, exist_ok=True)

    from test_execution import load_targets as _lt
    all_targets_map = _lt()
    all_targets = list(all_targets_map.values())

    # Filter to rows processed in this pipeline
    with open(os.path.join(OAI_DIR, "section1_patches", "metrics.json")) as f:
        s1_metrics = json.load(f)
    patch_row_nums = {int(k.replace("row", "")) for k in s1_metrics}
    targets = [t for t in all_targets if t["row_num"] in patch_row_nums]
    target_map = {t["row_num"]: t for t in targets}

    all_rows = []
    for target in targets:
        row_num = target["row_num"]
        row_key = f"row{row_num:02d}"

        for condition, dirs in CONDITIONS.items():
            s1_path = os.path.join(dirs["s1_dir"], "metrics.json")
            if not os.path.isfile(s1_path):
                continue
            with open(s1_path) as f:
                cond_s1 = json.load(f)
            if row_key not in cond_s1:
                continue

            row = collect_row(row_key, condition, dirs, target)
            all_rows.append(row)

    if not all_rows:
        print("ERROR: no data found — run sections 1–6 first")
        sys.exit(1)

    results_path = os.path.join(OUT_DIR, "results.csv")
    summary_path = os.path.join(OUT_DIR, "summary.csv")

    write_results_csv(all_rows, results_path)
    write_summary_csv(all_rows, summary_path)

    print(f"Wrote {results_path}  ({len(all_rows)} rows)")
    print(f"Wrote {summary_path}")

    print_summary(all_rows)

    print("\nSECTION 7 COMPLETE")


if __name__ == "__main__":
    main()
