#!/usr/bin/env python3
"""
Section 1: Patch Generation
Prompts GPT-4 for each entry in flaky_test_data_no_suspect.json and writes
raw responses to section1_patches/.
"""

import argparse
import json
import os
import time
from openai import OpenAI


from test_execution import ensure_openai_api_key


def get_client() -> OpenAI:
    api_key = ensure_openai_api_key()
    return OpenAI(api_key=api_key)


REPRO_BLOCK = (
    "REPRODUCTION STEPS:\n"
    "Steps required to reproduce failure: {reproduction_steps}\n\n"
)

PROMPT_TEMPLATE = """You are a software testing expert specializing in debugging and
repairing flaky tests.
Your task is to fix a flaky test using structured execution evidence.

TEST METADATA:
Flaky Type: {od_or_id}

ORDER-DEPENDENT CONTEXT:
Victim Test: {victim_test_name}
Polluter Test: {polluter_test_name}

FLAKINESS DESCRIPTION:
Order-dependent flaky tests fail due to interactions between tests through shared state. \
A polluter test modifies state that causes a victim test to fail. Fixes typically remove \
shared state dependencies or isolate tests.

{repro_block}ERROR INFORMATION:
Error Messages: {error_messages}
Failing Lines: {failing_lines}

CODE CONTEXT:
Relevant Global Variables: {global_variables}
Relevant Helper Methods: {helper_methods}
Relevant Test Code: {full_test_code}

INSTRUCTIONS - STRICT OUTPUT FORMAT:
Follow steps below. Output raw code only. Do NOT use markdown code fences (\`\`\`). \
Do NOT include \`\`\` anywhere in your output. Do not write explanations.

1) Fix the flakiness and print the fixed complete method code of this test between:
//<fix start> CODE HERE //<fix end>

Requirements:
• Code must compile and use correct argument and variable types.
• Do NOT invent new classes, methods or API that are not explicitly in the code context
• Do NOT suppress assertion failures with try-catch.
• Make minimal changes; preserve original test logic unless absolutely necessary.
• Fix nondeterminism at the source (e.g., ordering, shared state, API usage).
• Only wrap or modify the existing flaky API usage; do NOT bypass it.
• Do not manually reconstruct expected outputs or JSON strings.
• Only use reflection if it is already present in the test.

2) If dependencies must be updated, output:
<!-- <pom.xml start> -->
DEPENDENCY HERE
<!-- <pom.xml end> -->

Rules:
• Provide exact version.
• Do not duplicate existing dependencies.
• Do not include project artifacts.

3) If imports must be added, output:
//<import start> IMPORTS HERE //<import end>

Assume all existing classes and imports are already correctly configured."""


def format_reproduction_steps(steps):
    if isinstance(steps, list):
        return "\n".join(f"  {i+1}. {s}" for i, s in enumerate(steps))
    return str(steps)


def prompt_gpt4(entry: dict, include_repro: bool = True) -> dict:
    """
    Send one flaky_test_data entry to GPT-4 and return the response text plus usage metrics.

    Args:
        entry: A single dict from flaky_test_data_no_suspect.json testdata list.
        include_repro: Whether to include the REPRODUCTION STEPS block in the prompt.

    Returns:
        Dict with keys: response (str), prompt_tokens (int), completion_tokens (int),
        total_tokens (int), elapsed_seconds (float).
    """
    repro_block = (
        REPRO_BLOCK.format(
            reproduction_steps=format_reproduction_steps(entry.get("reproduction_steps", []))
        ) if include_repro else ""
    )
    gpt_prompt = PROMPT_TEMPLATE.format(
        od_or_id=entry.get("od_or_id", ""),
        victim_test_name=entry.get("victim_test_name", ""),
        polluter_test_name=entry.get("polluter_test_name", ""),
        repro_block=repro_block,
        error_messages=entry.get("error_messages", ""),
        failing_lines=entry.get("failing_lines", ""),
        global_variables=entry.get("global_variables", ""),
        helper_methods=entry.get("helper_methods", ""),
        full_test_code=entry.get("full_test_code", ""),
    )

    client = get_client()
    t0 = time.time()
    full_response = client.chat.completions.create(
        model="gpt-4",
        temperature=0.2,
        messages=[{"role": "user", "content": gpt_prompt}],
    )
    elapsed = time.time() - t0

    usage = full_response.usage
    return {
        "response": full_response.choices[0].message.content,
        "prompt": gpt_prompt,
        "prompt_tokens": usage.prompt_tokens,
        "completion_tokens": usage.completion_tokens,
        "total_tokens": usage.total_tokens,
        "elapsed_seconds": round(elapsed, 2),
    }


PATCHES_DIR = os.path.join(os.path.dirname(__file__), "section1_patches")
METRICS_FILE = os.path.join(PATCHES_DIR, "metrics.json")


def make_patch_filename(row_num: int) -> str:
    return os.path.join(PATCHES_DIR, f"row{row_num:02d}__gpt4_1.txt")


def make_prompt_filename(row_num: int) -> str:
    return os.path.join(PATCHES_DIR, f"row{row_num:02d}__initial_prompt.txt")


def write_patch_file(filepath: str, entry: dict, response: str) -> None:
    header = (
        f"victim: {entry.get('victim_test_name', '')}\n"
        f"polluter: {entry.get('polluter_test_name', '')}\n"
        f"source: {entry.get('source', '')}\n"
    )
    with open(filepath, "w", encoding="utf-8") as f:
        f.write(header)
        f.write("=== OUTPUT ===\n")
        f.write(response)


def load_metrics() -> dict:
    if os.path.isfile(METRICS_FILE):
        with open(METRICS_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_metrics(metrics: dict) -> None:
    with open(METRICS_FILE, "w", encoding="utf-8") as f:
        json.dump(metrics, f, indent=2)


def print_metrics_summary(metrics: dict) -> None:
    print(f"\n{'Row':<6} {'Victim (short)':<45} {'Prompt':>8} {'Completion':>12} {'Total':>8} {'Time(s)':>8}")
    print("-" * 95)
    total_prompt = total_completion = total_tokens = total_time = 0
    for key in sorted(metrics):
        m = metrics[key]
        short = m["victim_test_name"].rsplit(".", 1)[-1][:44]
        print(f"{key:<6} {short:<45} {m['prompt_tokens']:>8} {m['completion_tokens']:>12} {m['total_tokens']:>8} {m['elapsed_seconds']:>8.1f}")
        total_prompt += m["prompt_tokens"]
        total_completion += m["completion_tokens"]
        total_tokens += m["total_tokens"]
        total_time += m["elapsed_seconds"]
    print("-" * 95)
    print(f"{'TOTAL':<6} {'':<45} {total_prompt:>8} {total_completion:>12} {total_tokens:>8} {total_time:>8.1f}")


def parse_args():
    parser = argparse.ArgumentParser(description="Generate GPT-4 patches for flaky tests.")
    parser.add_argument(
        "--rows", type=str, default=None,
        help="Rows to process, e.g. '1,5,10' or '1-10'."
    )
    parser.add_argument(
        "--limit", type=int, default=None,
        help="Process only the first N entries (default: all entries)."
    )
    parser.add_argument(
        "--start", type=int, default=1,
        help="1-based start row (used with --limit, default: 1)."
    )
    parser.add_argument(
        "--ablation", action="store_true",
        help="Exclude REPRODUCTION STEPS from the prompt (ablation study)."
    )
    return parser.parse_args()


def parse_rows_arg(s):
    """Parse '1,5,10' or '1-10' into a list of ints."""
    rows = []
    for part in s.split(","):
        part = part.strip()
        if "-" in part:
            a, b = part.split("-", 1)
            rows.extend(range(int(a), int(b) + 1))
        else:
            rows.append(int(part))
    return rows


def main():
    args = parse_args()
    global PATCHES_DIR, METRICS_FILE
    if args.ablation:
        PATCHES_DIR  = os.path.join(os.path.dirname(__file__), "section1_patches_ablation")
        METRICS_FILE = os.path.join(PATCHES_DIR, "metrics.json")
    os.makedirs(PATCHES_DIR, exist_ok=True)
    metrics = load_metrics()

    # Load targets from manifest (authoritative row numbering)
    from test_execution import load_targets, load_entry_for_target
    target_map = load_targets()

    # Determine which rows to process
    if args.rows:
        row_nums = parse_rows_arg(args.rows)
    elif args.limit is not None:
        row_nums = list(range(args.start, args.start + args.limit))
    else:
        row_nums = sorted(target_map.keys())

    # Filter to rows that exist in the manifest
    row_nums = [r for r in row_nums if r in target_map]
    total = len(row_nums)

    variant_tag = " [ABLATION]" if args.ablation else ""
    print(f"Generating patches for {total} entries{variant_tag}...\n")

    for idx, row_num in enumerate(row_nums):
        row_key = f"row{row_num:02d}"
        filepath = make_patch_filename(row_num)
        target = target_map[row_num]

        if os.path.isfile(filepath) and row_key in metrics:
            print(f"[{idx + 1}/{total}] SKIP (already exists): {os.path.basename(filepath)}")
            continue

        # Look up the entry by victim name to ensure correct match
        entry = load_entry_for_target(target)

        print(f"[{idx + 1}/{total}] Prompting GPT-4 for: {entry['victim_test_name']}", flush=True)
        try:
            result = prompt_gpt4(entry, include_repro=not args.ablation)
            write_patch_file(filepath, entry, result["response"])
            with open(make_prompt_filename(row_num), "w", encoding="utf-8") as pf:
                pf.write(result["prompt"])
            metrics[row_key] = {
                "victim_test_name": entry["victim_test_name"],
                "polluter_test_name": entry["polluter_test_name"],
                "prompt_tokens": result["prompt_tokens"],
                "completion_tokens": result["completion_tokens"],
                "total_tokens": result["total_tokens"],
                "elapsed_seconds": result["elapsed_seconds"],
            }
            save_metrics(metrics)
            print(f"      Saved -> {os.path.basename(filepath)} "
                  f"| tokens: {result['total_tokens']} "
                  f"| time: {result['elapsed_seconds']}s", flush=True)
        except Exception as e:
            print(f"      ERROR: {e}", flush=True)

    print_metrics_summary(metrics)


if __name__ == "__main__":
    main()
