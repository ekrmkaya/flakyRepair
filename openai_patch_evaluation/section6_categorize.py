#!/usr/bin/env python3
"""
Section 6: Categorization
Assigns a category to each row × condition (with_repro, no_repro).

Categories (priority order):
  Did not generate fix        — parse failed
  Compilation error           — never compiled across all attempts
  Did not address flakiness   — compiled but test still failed
  Incorrect Logic             — NEEDS_REVIEW: bare catch / missing calls / >50% body change
  Assertion weakened          — NEEDS_REVIEW: original assertions absent from patch
  Fixed flakiness             — compiled and test passed

Output per row × condition:  section6_categories/rowXX_{condition}/
  category.txt
  passed.txt          (Y | N)
  needs_review.txt    (true | false)
"""

import json
import os
import re
import subprocess
import sys

BASE_DIR     = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # repository root
OAI_DIR      = os.path.dirname(os.path.abspath(__file__))
OUT_DIR      = os.path.join(OAI_DIR, "section6_categories")

CONDITIONS = {
    "with_repro": {
        "s1_dir":  os.path.join(OAI_DIR, "section1_patches"),
        "s2_dir":  os.path.join(OAI_DIR, "section2_parsed"),
        "s4_dir":  os.path.join(OAI_DIR, "section4_compilation"),
        "s5_dir":  os.path.join(OAI_DIR, "section5_test_runs"),
    },
    "no_repro": {
        "s1_dir":  os.path.join(OAI_DIR, "section1_patches_no_repro"),
        "s2_dir":  os.path.join(OAI_DIR, "section2_parsed_no_repro"),
        "s4_dir":  os.path.join(OAI_DIR, "section4_compilation_no_repro"),
        "s5_dir":  os.path.join(OAI_DIR, "section5_test_runs_no_repro"),
    },
}


# ── file helpers ──────────────────────────────────────────────────────────────

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


# ── locate winning patched file ───────────────────────────────────────────────

def find_winning_patched_file(s5_row_dir):
    """Return path to the patched file from the FIXED attempt, or None."""
    if not os.path.isdir(s5_row_dir):
        return None
    for name in sorted(os.listdir(s5_row_dir)):
        if not name.startswith("attempt"):
            continue
        attempt_dir = os.path.join(s5_row_dir, name)
        if not os.path.isdir(attempt_dir):
            continue
        result = rfile(attempt_dir, "attempt_result.txt")
        if result != "FIXED":
            continue
        # Prefer stitched file if stitch compiled successfully
        stitched = os.path.join(attempt_dir, "stitched_test.java")
        stitch_status = rfile(attempt_dir, "stitch_compile_status.txt")
        if os.path.isfile(stitched) and stitch_status == "PASS":
            return stitched
        patched = os.path.join(attempt_dir, "patched_test.java")
        if os.path.isfile(patched):
            return patched
    return None


# ── heuristics (mirrored from patch_evaluation/section6_categorize.py) ───────

def find_method_in_source(lines, method_name):
    sig_re = re.compile(
        r'^\s*(public|protected|private)?\s*(static\s+)?void\s+' +
        re.escape(method_name) + r'\s*\('
    )
    for i, line in enumerate(lines):
        if sig_re.search(line):
            ann_start = i
            j = i - 1
            while j >= 0 and re.match(r'^\s*@', lines[j].rstrip()):
                ann_start = j
                j -= 1
            depth = 0
            started = False
            end_idx = i
            for k in range(i, len(lines)):
                for ch in lines[k]:
                    if ch == '{':
                        depth += 1
                        started = True
                    elif ch == '}':
                        depth -= 1
                if started and depth == 0:
                    end_idx = k
                    break
            return ann_start, end_idx
    return None, None


def extract_method_lines(text, method_name):
    lines = text.splitlines(keepends=True)
    start, end = find_method_in_source(lines, method_name)
    if start is None:
        return []
    return lines[start:end + 1]


def method_body_lines(method_lines):
    depth = 0
    started = False
    body = []
    for line in method_lines:
        for ch in line:
            if ch == '{':
                depth += 1
                started = True
            elif ch == '}':
                depth -= 1
        if started and depth > 0:
            body.append(line)
    return body


def rule_incorrect_logic(orig_text, patch_text, victim_method):
    orig_lines  = extract_method_lines(orig_text,  victim_method)
    patch_lines = extract_method_lines(patch_text, victim_method)
    if not orig_lines or not patch_lines:
        return False, ""

    patch_block     = "".join(patch_lines)
    orig_body_lines = method_body_lines(orig_lines)
    patch_body_lines = method_body_lines(patch_lines)

    # Bare catch swallowing exceptions
    if re.search(r'catch\s*\([^)]+\)\s*\{\s*(//[^\n]*)?\s*\}', patch_block):
        return True, "bare catch block swallowing exceptions"

    # >50% body line count change
    orig_len  = len(orig_body_lines)
    patch_len = len(patch_body_lines)
    if orig_len > 0 and abs(patch_len - orig_len) / orig_len > 0.50:
        pct = int(abs(patch_len - orig_len) / orig_len * 100)
        return True, f"body line count changed {pct}% (orig={orig_len}, patch={patch_len})"

    # Method calls in original absent from patch
    java_kw = {"if","for","while","switch","catch","new","return","throw","assert",
               "super","this","void","boolean","int","long","double","float",
               "String","Object","Class","List","Map","Set","ArrayList","HashMap"}
    def calls(lines_list):
        return set(re.findall(r'\b([a-zA-Z_]\w*)\s*\(', "".join(lines_list))) - java_kw
    missing = calls(orig_lines) - calls(patch_lines)
    if missing:
        return True, f"original API calls absent from patch: {sorted(missing)}"

    return False, ""


def rule_assertion_weakened(orig_text, patch_text, victim_method):
    orig_lines  = extract_method_lines(orig_text,  victim_method)
    patch_lines = extract_method_lines(patch_text, victim_method)
    if not orig_lines:
        return False, []

    assert_re = re.compile(
        r'assert(Equals|True|False|NotNull|Null|Same|NotSame|That|'
        r'ArrayEquals|ThatHamcrest|ThrowsExactly)\s*\('
    )
    orig_asserts = {
        line.strip().rstrip(";").rstrip()
        for line in orig_lines
        if assert_re.search(line.strip())
    }
    if not orig_asserts:
        return False, []

    patch_block = "".join(patch_lines)
    missing = [a for a in orig_asserts if a not in patch_block]
    return bool(missing), missing


# ── categorize one row × condition ───────────────────────────────────────────

def categorize(row_key, target, dirs):
    s2_row  = os.path.join(dirs["s2_dir"], row_key)
    s5_row  = os.path.join(dirs["s5_dir"], row_key)

    parse_status = rfile(s2_row, "parse_status.txt")
    # Support both new (row_result.json) and old (final_status.txt) formats
    row_result_path = os.path.join(s5_row, "row_result.json")
    if os.path.isfile(row_result_path):
        try:
            import json as _json
            with open(row_result_path) as _f:
                final_status = _json.load(_f).get("final_status", "")
        except Exception:
            final_status = ""
    else:
        final_status = rfile(s5_row, "final_status.txt")

    # Rule 1: did not generate fix
    if parse_status != "OK":
        return "Did not generate fix", "N", False

    # Rule 2: compilation error
    if final_status == "COMPILE_ERROR":
        return "Compilation error", "N", False

    # Rule 3: skipped upstream
    if final_status == "SKIPPED" or not final_status:
        return "Skipped", "N", False

    # Rule 4: did not address flakiness
    if final_status == "NOT_FIXED":
        return "Did not address flakiness", "N", False

    # FIXED — run heuristics on winning patched file
    if final_status == "FIXED":
        patched_file = find_winning_patched_file(s5_row)
        test_src_abs = os.path.join(BASE_DIR, target.get("test_src_path", ""))
        if patched_file and os.path.isfile(patched_file) and os.path.isfile(test_src_abs):
            orig_text   = read_file(test_src_abs)
            patch_text  = read_file(patched_file)
            victim_meth = target["victim_method"]

            fires_logic, reason_logic = rule_incorrect_logic(orig_text, patch_text, victim_meth)
            if fires_logic:
                return f"NEEDS_REVIEW:Incorrect Logic ({reason_logic})", "N", True

            fires_assert, missing_assert = rule_assertion_weakened(orig_text, patch_text, victim_meth)
            if fires_assert:
                detail = "; ".join(missing_assert[:3])
                return f"NEEDS_REVIEW:Assertion weakened ({detail})", "N", True

        return "Fixed flakiness", "Y", False

    return "UNCATEGORIZED", "N", False


# ── write / read output ───────────────────────────────────────────────────────

def write_output(row_key, condition, category, passed, needs_review):
    out = os.path.join(OUT_DIR, f"{row_key}_{condition}")
    os.makedirs(out, exist_ok=True)
    for name, val in [
        ("category.txt",     category),
        ("passed.txt",       passed),
        ("needs_review.txt", str(needs_review).lower()),
    ]:
        with open(os.path.join(out, name), "w") as f:
            f.write(val + "\n")


def validate():
    failures = []
    for d in os.listdir(OUT_DIR):
        dpath = os.path.join(OUT_DIR, d)
        if not os.path.isdir(dpath):
            continue
        for fname in ("category.txt", "passed.txt", "needs_review.txt"):
            if not os.path.isfile(os.path.join(dpath, fname)):
                failures.append(f"{d}: missing {fname}")
        cat = rfile(dpath, "category.txt")
        if "NEEDS_REVIEW" in cat:
            failures.append(f"{d}: unresolved NEEDS_REVIEW: {cat}")
        if cat == "UNCATEGORIZED":
            failures.append(f"{d}: UNCATEGORIZED")
    return failures


# ── main ─────────────────────────────────────────────────────────────────────

def parse_args():
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--only",          help="Process only this row key, e.g. row01")
    p.add_argument("--validate-only", action="store_true")
    return p.parse_args()


def main():
    args = parse_args()

    if args.validate_only:
        failures = validate()
        if failures:
            for msg in failures:
                print(f"  FAIL: {msg}")
            print(f"\nSECTION 6 BLOCKED: {len(failures)} issue(s)")
            sys.exit(1)
        print("All categories resolved.")
        sys.exit(0)

    print("=" * 60)
    print("SECTION 6: CATEGORIZATION")
    print("=" * 60)

    os.makedirs(OUT_DIR, exist_ok=True)

    from test_execution import load_targets as _lt
    all_targets_map = _lt()
    all_targets = list(all_targets_map.values())

    # Only process rows present in section1 metrics
    with open(os.path.join(OAI_DIR, "section1_patches", "metrics.json")) as f:
        s1_metrics = json.load(f)
    patch_row_nums = {int(k.replace("row", "")) for k in s1_metrics}
    targets = [t for t in all_targets if t["row_num"] in patch_row_nums]

    if args.only:
        targets = [t for t in targets if t["test_id"].startswith(args.only)]
        if not targets:
            print(f"ERROR: no target matching {args.only!r}")
            sys.exit(1)

    print(f"Processing {len(targets)} target(s) × 2 conditions...\n")

    needs_review_list = []
    cat_counts = {}
    results = []

    for target in targets:
        row_num = target["row_num"]
        row_key = f"row{row_num:02d}"

        for condition, dirs in CONDITIONS.items():
            # Check if this condition has data
            s1_metrics_path = os.path.join(dirs["s1_dir"], "metrics.json")
            if not os.path.isfile(s1_metrics_path):
                continue
            with open(s1_metrics_path) as f:
                cond_metrics = json.load(f)
            if row_key not in cond_metrics:
                continue

            cat, passed, needs_rev = categorize(row_key, target, dirs)
            write_output(row_key, condition, cat, passed, needs_rev)

            base_cat = cat.split(":")[0] if "NEEDS_REVIEW" in cat else cat
            cat_counts[base_cat] = cat_counts.get(base_cat, 0) + 1

            if needs_rev:
                needs_review_list.append((row_key, condition, cat))

            results.append((row_key, condition, cat, passed))
            flag = " *** NEEDS_REVIEW" if needs_rev else ""
            print(f"  {row_key} [{condition:10}] → {cat}{flag}", flush=True)

    # Summary
    print("\n" + "-" * 60)
    for cat, count in sorted(cat_counts.items(), key=lambda x: -x[1]):
        print(f"  {cat:<45} {count}")

    passed_count = sum(1 for _, _, _, p in results if p == "Y")
    print(f"\nTotal: {len(results)} | Passed: {passed_count} | Needs review: {len(needs_review_list)}")

    if needs_review_list:
        print(f"\nACTION REQUIRED — resolve these before running Section 7:")
        for rk, cond, cat in needs_review_list:
            out = os.path.join(OUT_DIR, f"{rk}_{cond}")
            print(f"  {rk} [{cond}]: {cat}")
            print(f"    Edit: {out}/category.txt  and  needs_review.txt")
        print("\n  Then re-run:  python3.9 section6_categorize.py --validate-only")

    print("\nValidating outputs...")
    failures = validate()
    if failures:
        for msg in failures:
            print(f"  FAIL: {msg}")
        if needs_review_list:
            print("\nSECTION 6 BLOCKED: manual review required")
        else:
            print(f"\nSECTION 6 FAILED: {failures[0]}")
        sys.exit(1)

    print("\nSECTION 6 COMPLETE")


if __name__ == "__main__":
    main()
