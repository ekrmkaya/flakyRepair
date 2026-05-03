#!/usr/bin/env python3
"""
Section 2: Patch Parsing
Reads raw GPT-4 patch files from section1_patches/, extracts fix_code,
imports, and pom_snippet. Writes results to section2_parsed/row{N}/.

Output per row:
  section2_parsed/row{N}/
    raw_output.txt    — everything after "=== OUTPUT ===" in the patch file
    fix_code.java     — extracted method code
    imports.txt       — new import statements (may be empty)
    pom_snippet.xml   — pom dependency block (may be empty)
    parse_status.txt  — OK | NO_FIX_BLOCK | EMPTY_OUTPUT
"""

import argparse
import json
import os
import re

BASE_DIR   = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # repository root
OAI_DIR    = os.path.dirname(os.path.abspath(__file__))
PATCHES_DIR = os.path.join(OAI_DIR, "section1_patches")
METRICS_FILE = os.path.join(PATCHES_DIR, "metrics.json")
OUT_DIR    = os.path.join(OAI_DIR, "section2_parsed")


# ── parsing helpers (mirrors patch_evaluation/section2_parse_patches.py) ─────

def extract_block(text, start_marker, end_marker):
    start_idx = text.find(start_marker)
    if start_idx == -1:
        return ""
    start_idx += len(start_marker)
    end_idx = text.find(end_marker, start_idx)
    if end_idx == -1:
        return ""
    return text[start_idx:end_idx]


def strip_blank_lines(text):
    lines = text.split("\n")
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    return "\n".join(lines)


def is_backtick_only(text):
    stripped = text.replace("\n", "").replace("\r", "").replace(" ", "").replace("\t", "")
    return len(stripped) > 0 and all(c == "`" for c in stripped)


def determine_parse_status(output_section, fix_code):
    has_fix_marker = "//<fix start>" in output_section or "<fix start>" in output_section
    if not has_fix_marker:
        if is_backtick_only(output_section) or not output_section.strip():
            return "EMPTY_OUTPUT"
        return "NO_FIX_BLOCK"
    if not fix_code or not fix_code.strip() or is_backtick_only(fix_code):
        return "NO_FIX_BLOCK"
    return "OK"


def parse_patch_file(filepath):
    """
    Read a section1_patches file and return
    (raw_output, fix_code, imports, pom_snippet, parse_status).
    """
    with open(filepath, encoding="utf-8", errors="replace") as f:
        content = f.read()

    output_marker = "=== OUTPUT ==="
    idx = content.find(output_marker)
    if idx == -1:
        return "", "", "", "", "EMPTY_OUTPUT"

    raw_output = content[idx + len(output_marker):]

    # Accept both "//<marker>" (GPT-4) and "<marker>" variants
    fix_code = (extract_block(raw_output, "//<fix start>", "//<fix end>") or
                extract_block(raw_output, "<fix start>",   "<fix end>")   or
                extract_block(raw_output, "<fix start>",   "</fix end>"))
    imports  = (extract_block(raw_output, "//<import start>", "//<import end>") or
                extract_block(raw_output, "<import start>",   "<import end>")   or
                extract_block(raw_output, "<import start>",   "</import end>"))
    pom_snippet = (extract_block(raw_output, "<!-- <pom.xml start> -->", "<!-- <pom.xml end> -->") or
                   extract_block(raw_output, "<pom.xml start>",          "<pom.xml end>")          or
                   extract_block(raw_output, "<pom.xml start>",          "</pom.xml end>"))

    fix_code = strip_blank_lines(fix_code)
    imports  = imports.strip()

    parse_status = determine_parse_status(raw_output, fix_code)
    return raw_output, fix_code, imports, pom_snippet, parse_status


def write_parsed(row_key, raw_output, fix_code, imports, pom_snippet, parse_status):
    out = os.path.join(OUT_DIR, row_key)
    os.makedirs(out, exist_ok=True)

    def w(name, val):
        with open(os.path.join(out, name), "w", encoding="utf-8") as f:
            f.write(val)

    w("raw_output.txt",   raw_output)
    w("fix_code.java",    fix_code)
    w("imports.txt",      imports)
    w("pom_snippet.xml",  pom_snippet)
    w("parse_status.txt", parse_status)


def load_metrics():
    if not os.path.isfile(METRICS_FILE):
        print(f"SECTION 2 FAILED: {METRICS_FILE} not found — run Section 1 first")
        import sys; import sys; exit(1)
    with open(METRICS_FILE, encoding="utf-8") as f:
        return json.load(f)


def parse_args():
    parser = argparse.ArgumentParser(description="Parse GPT-4 patch files.")
    parser.add_argument("--only", help="Process only this row key, e.g. row01")
    parser.add_argument("--no-repro", action="store_true",
                        help="Read from section1_patches_no_repro/ (no-repro variant).")
    return parser.parse_args()


def main():
    args = parse_args()
    global PATCHES_DIR, METRICS_FILE, OUT_DIR
    if args.no_repro:
        PATCHES_DIR  = os.path.join(OAI_DIR, "section1_patches_no_repro")
        METRICS_FILE = os.path.join(PATCHES_DIR, "metrics.json")
        OUT_DIR      = os.path.join(OAI_DIR, "section2_parsed_no_repro")
    os.makedirs(OUT_DIR, exist_ok=True)

    metrics = load_metrics()
    row_keys = sorted(metrics.keys())
    if args.only:
        if args.only not in row_keys:
            print(f"ERROR: {args.only} not found in metrics")
            exit(1)
        row_keys = [args.only]

    variant_tag = " [NO-REPRO]" if args.no_repro else ""
    print("=" * 60)
    print(f"SECTION 2: PATCH PARSING{variant_tag}")
    print("=" * 60)
    print(f"Processing {len(row_keys)} patch(es)...\n")

    results = []
    for row_key in row_keys:
        patch_file = os.path.join(PATCHES_DIR, f"{row_key}__gpt4_1.txt")

        # Skip if already parsed
        existing_status = os.path.join(OUT_DIR, row_key, "parse_status.txt")
        if os.path.isfile(existing_status):
            with open(existing_status) as f:
                cached = f.read().strip()
            print(f"  {row_key}: SKIP (already parsed: {cached})")
            results.append((row_key, cached))
            continue

        if not os.path.isfile(patch_file):
            print(f"  {row_key}: WARNING — patch file not found: {patch_file}")
            write_parsed(row_key, "", "", "", "", "EMPTY_OUTPUT")
            results.append((row_key, "EMPTY_OUTPUT"))
            continue

        raw_output, fix_code, imports, pom_snippet, parse_status = parse_patch_file(patch_file)
        write_parsed(row_key, raw_output, fix_code, imports, pom_snippet, parse_status)
        print(f"  {row_key}: {parse_status}", flush=True)
        results.append((row_key, parse_status))

    # Summary
    counts = {"OK": 0, "NO_FIX_BLOCK": 0, "EMPTY_OUTPUT": 0}
    for _, status in results:
        counts[status] = counts.get(status, 0) + 1

    print(f"\nParse Results:")
    print(f"  OK            : {counts['OK']}")
    print(f"  NO_FIX_BLOCK  : {counts['NO_FIX_BLOCK']}")
    print(f"  EMPTY_OUTPUT  : {counts['EMPTY_OUTPUT']}")

    non_ok = [(k, s) for k, s in results if s != "OK"]
    if non_ok:
        print("\nNon-OK rows:")
        for k, s in non_ok:
            print(f"  {k}: {s}")
    else:
        print("\nAll patches parsed successfully.")

    print("\nSECTION 2 COMPLETE")


if __name__ == "__main__":
    main()
