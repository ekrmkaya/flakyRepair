#!/usr/bin/env python3
"""
Section 5: Test Execution with Retry Loop

For each row:
  1. Attempt 1: Use the best compiled patch from section 4.
     - If section 4 final_compile_status == FAIL, attempt 1 is consumed
       (stitch also failed there) and the loop starts at attempt 2.
     - If section 4 final_compile_status == PASS, attempt 1 runs the test.
  2. Attempts 2-5 (up to MAX_ATTEMPTS total): Re-prompt GPT-4 with retry
     context (original prompt + previous patch + error), compile, stitch if
     needed, and run the test if compiled.
  3. Termination:
     - FIXED:         victim test passes in polluter->victim order.
     - NOT_FIXED:     patch compiled but test still fails after max attempts.
     - COMPILE_ERROR: patch never compiled after all attempts.
     - Early exit:    same error string appears SAME_ERROR_LIMIT times in a row.
     - SKIPPED:       row excluded upstream (parse failed, etc.)

Output per attempt:  section5_test_runs/rowXX/attempt{N}/
  attempt.json            structured status + fix_code + imports
  patched_test.java       final patched source (stitched if stitch succeeded)
  compile.log             final compile output
  test_run.log            OD test output (if compiled)
  victim_alone.log        victim isolation (if test PASSED)
  polluter_alone.log      polluter isolation (if test PASSED)
  gpt4_retry.txt          retry prompt + response (attempts 2+)
  gpt4_stitch.txt         stitch prompt + response (if stitch ran)

Output per row:  section5_test_runs/rowXX/
  row_result.json         final_status + attempt_metrics

section5_test_runs/metrics.json -- per-row summary
"""

import argparse
import json
import os
import re
import subprocess
import sys
import time

from test_execution import (
    run_cmd,
    run_od_test,
    run_victim_alone,
    run_polluter_alone,
    make_env,
    load_targets,
    load_entry_for_target,
    ensure_openai_api_key,
    SKIP_FLAGS,
    COMPILE_TIMEOUT,
)

OAI_DIR      = os.path.dirname(os.path.abspath(__file__))
METRICS_S1   = os.path.join(OAI_DIR, "section1_patches", "metrics.json")
COMP_DIR     = os.path.join(OAI_DIR, "section4_compilation")
PARSED_DIR   = os.path.join(OAI_DIR, "section2_parsed")
OUT_DIR      = os.path.join(OAI_DIR, "section5_test_runs")
METRICS_FILE = os.path.join(OUT_DIR, "metrics.json")

MAX_ATTEMPTS     = 5
SAME_ERROR_LIMIT = 3

# Base path for resolving relative paths in targets
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # repository root


# -- prompt suffixes -----------------------------------------------------------

RETRY_COMPILE_SUFFIX = """

--------------------------------------------------------------------------------
PREVIOUS ATTEMPT FAILED TO COMPILE
--------------------------------------------------------------------------------

A fix was generated from the instructions above and applied to the test file,
but it did NOT compile. Below is the exact state that was compiled and the
resulting errors so you can identify what needs to change.

Fix code that was applied:
//<attempted fix start>
{attempted_fix_code}
//<attempted fix end>

Imports that were added to the file (empty means none were added):
//<attempted imports start>
{attempted_imports}
//<attempted imports end>

Compiler errors (most relevant lines):
{compiler_errors}

Please provide a corrected version that compiles successfully.

Rules:
- Output in exactly the same format as before (//<fix start> ... //<fix end>, etc.).
- The corrected code MUST compile without errors.
- Preserve the original fix intent; only change what is necessary to compile.
- Do NOT add try-catch blocks around assertions to suppress failures.
- If the fix requires different or additional imports, include them in \
//<import start> ... //<import end>.
- Do NOT invent new classes, methods or API that are not explicitly in the code context.
"""

RETRY_TEST_SUFFIX = """

--------------------------------------------------------------------------------
PREVIOUS ATTEMPT COMPILED BUT DID NOT FIX THE FLAKINESS
--------------------------------------------------------------------------------

A fix was generated, applied, and compiled successfully, but running the test
in polluter->victim order shows the victim test still fails. Below is the fix
that was attempted and the relevant test failure output.

Fix code that was applied:
//<attempted fix start>
{attempted_fix_code}
//<attempted fix end>

Imports that were added to the file (empty means none were added):
//<attempted imports start>
{attempted_imports}
//<attempted imports end>

Test failure output (relevant lines):
{test_failure_excerpt}

Please provide a DIFFERENT fix that addresses the root cause of the flakiness.

Rules:
- Output in exactly the same format as before (//<fix start> ... //<fix end>, etc.).
- The corrected code MUST compile without errors.
- Do NOT repeat the same fix -- it did not address the flakiness.
- Do NOT add try-catch blocks around assertions to suppress failures.
- Make minimal changes; preserve original test logic unless absolutely necessary.
- Fix nondeterminism at the source (e.g., ordering, shared state, API usage).
- Do NOT invent new classes, methods or API that are not explicitly in the code context.
- If imports must be added, include them in //<import start> ... //<import end>.
"""

STITCH_PROMPT_SUFFIX = """

--------------------------------------------------------------------------------
PREVIOUS ATTEMPT FAILED TO COMPILE
--------------------------------------------------------------------------------

A fix was generated from the instructions above and applied to the test file,
but it did NOT compile. Below is the exact state that was compiled and the
resulting errors so you can identify what needs to change.

Fix code that was applied:
//<attempted fix start>
{attempted_fix_code}
//<attempted fix end>

Imports that were added to the file (empty means none were added):
//<attempted imports start>
{attempted_imports}
//<attempted imports end>

Compiler errors (most relevant lines):
{compiler_errors}

Please provide a corrected version that compiles successfully.

Rules:
- Output in exactly the same format as before (//<fix start> ... //<fix end>, etc.).
- The corrected code MUST compile without errors.
- Preserve the original fix intent; only change what is necessary to compile.
- Do NOT add try-catch blocks around assertions to suppress failures.
- If the fix requires different or additional imports, include them in \
//<import start> ... //<import end>.
- Do NOT invent new classes, methods or API that are not explicitly in the code context.
"""


# -- I/O helpers ---------------------------------------------------------------

def read_file(path):
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except Exception:
        return ""


def write_file(path, content):
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


def compile_status_from_output(rc, output, test_src_rel):
    if rc != 0:
        return "FAIL"
    filename = os.path.basename(test_src_rel)
    for line in output.splitlines():
        if "[ERROR]" in line and filename in line:
            return "FAIL"
    return "PASS"


def extract_compiler_errors(output, test_src_rel, max_errors=15, source_content=None):
    filename  = os.path.basename(test_src_rel)
    error_re  = re.compile(
        r'\[ERROR\]\s+\S+?' + re.escape(filename) + r':\[(\d+),\d+\]\s+(.+)'
    )
    src_lines = source_content.splitlines() if source_content else None

    seen    = set()
    results = []
    for line in output.splitlines():
        m = error_re.search(line)
        if m:
            line_num  = int(m.group(1))
            error_msg = m.group(2).strip()
            key = f"{filename}:[{line_num}] {error_msg}"
            if key not in seen:
                seen.add(key)
                entry = key
                if src_lines and 1 <= line_num <= len(src_lines):
                    entry += f"\n  -> {src_lines[line_num - 1].strip()}"
                results.append(entry)
            if len(results) >= max_errors:
                break
    if not results:
        skip = ("execute goal", "-> [Help", "stack trace", "Re-run Maven",
                "re-run Maven", "For more information", "[Help 1]", "COMPILATION ERROR")
        for line in output.splitlines():
            s = line.strip()
            if (s.startswith("[ERROR]")
                    and not any(p in s for p in skip)):
                results.append(re.sub(r'^\[ERROR\]\s*', '', s))
    return "\n".join(results)


def extract_test_failure_excerpt(output, max_lines=20):
    lines = output.splitlines()
    relevant = []
    keywords = [
        "FAILED", "AssertionError", "Exception:", "Error:",
        "<<< FAILURE", "<<< ERROR", "junit.framework", "org.junit",
        "expected:", "but was:", "BUILD FAILURE",
    ]
    for i, line in enumerate(lines):
        if any(kw in line for kw in keywords):
            start = max(0, i - 1)
            end   = min(len(lines), i + 3)
            relevant.extend(lines[start:end])
            relevant.append("")
    if not relevant:
        relevant = lines[-max_lines:]
    seen = set()
    deduped = []
    for l in relevant:
        if l not in seen:
            seen.add(l)
            deduped.append(l)
    return "\n".join(deduped[:max_lines])


def restore_source(target):
    test_src_rel = target.get("test_src_path")
    if not test_src_rel:
        return
    test_src_abs = os.path.join(BASE_DIR, test_src_rel)
    repo_abs     = os.path.join(BASE_DIR, target["repo_local_path"])
    file_in_repo = os.path.relpath(test_src_abs, repo_abs)
    try:
        subprocess.run(
            ["git", "checkout", "--", file_in_repo],
            cwd=repo_abs, capture_output=True, timeout=5
        )
    except Exception:
        pass


# -- data loading --------------------------------------------------------------

def load_global_metrics():
    if os.path.isfile(METRICS_FILE):
        with open(METRICS_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_global_metrics(metrics):
    with open(METRICS_FILE, "w", encoding="utf-8") as f:
        json.dump(metrics, f, indent=2)


# -- section 4 state readers ---------------------------------------------------

def s4_row_metrics(row_key):
    s4m_file = os.path.join(COMP_DIR, "metrics.json")
    if not os.path.isfile(s4m_file):
        return {}
    with open(s4m_file, encoding="utf-8") as f:
        return json.load(f).get(row_key, {})


def s4_skip_reason(row_key):
    p = os.path.join(COMP_DIR, row_key, "skip_reason.txt")
    if not os.path.isfile(p):
        return "MISSING"
    return read_file(p).strip()


def s4_best_patch(row_key):
    comp_row = os.path.join(COMP_DIR, row_key)

    def read_stat(fname):
        p = os.path.join(comp_row, fname)
        return read_file(p).strip() if os.path.isfile(p) else "NA"

    use_stitch = read_stat("stitch_compile_status.txt") == "PASS"
    use_initial = read_stat("compile_status.txt") == "PASS"

    if use_stitch:
        pf    = os.path.join(comp_row, "stitched_test.java")
        label = "section4_stitched"
        sr    = os.path.join(comp_row, "stitch_raw_response.txt")
        if os.path.isfile(sr):
            fc, im, ps = parse_gpt4_response(read_file(sr))
            if ps == "OK":
                return read_file(pf), label, fc, im
    elif use_initial:
        pf    = os.path.join(comp_row, "patched_test.java")
        label = "section4_initial"
    else:
        return None, None, None, None

    fc_path = os.path.join(PARSED_DIR, row_key, "fix_code.java")
    im_path = os.path.join(PARSED_DIR, row_key, "imports.txt")
    fc = read_file(fc_path).strip() if os.path.isfile(fc_path) else ""
    im = read_file(im_path).strip() if os.path.isfile(im_path) else ""
    return read_file(pf), label, fc, im


def s4_last_compile_errors(row_key, test_src_rel):
    comp_row = os.path.join(COMP_DIR, row_key)
    src_content = None
    for patch_fname in ("stitched_test.java", "patched_test.java"):
        p = os.path.join(comp_row, patch_fname)
        if os.path.isfile(p):
            src_content = read_file(p)
            break
    for log_name in ("stitch_compile.log", "compile.log"):
        log_path = os.path.join(comp_row, log_name)
        if os.path.isfile(log_path):
            output = read_file(log_path)
            errors = extract_compiler_errors(output, test_src_rel, source_content=src_content)
            if errors:
                return errors
    return "(compile log exists but no errors could be extracted)"


# -- GPT-4 integration --------------------------------------------------------

def call_gpt4(prompt):
    from openai import OpenAI
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY not set")
    client = OpenAI(api_key=api_key)
    t0 = time.time()
    resp = client.chat.completions.create(
        model="gpt-4",
        temperature=0.2,
        messages=[{"role": "user", "content": prompt}],
    )
    elapsed = round(time.time() - t0, 2)
    usage = resp.usage
    return (
        resp.choices[0].message.content,
        usage.prompt_tokens,
        usage.completion_tokens,
        usage.total_tokens,
        elapsed,
    )


def load_entry_for_row(row_key, target):
    """Load the flaky_test_data entry for this row, matched by victim name."""
    return load_entry_for_target(target)


def build_original_prompt(entry, include_repro=True):
    from section1_generate_patches import PROMPT_TEMPLATE, REPRO_BLOCK, format_reproduction_steps
    repro_block = (
        REPRO_BLOCK.format(
            reproduction_steps=format_reproduction_steps(entry.get("reproduction_steps", []))
        ) if include_repro else ""
    )
    return PROMPT_TEMPLATE.format(
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


def parse_gpt4_response(response_text):
    from section2_parse_patches import extract_block, strip_blank_lines, determine_parse_status
    fix_code = (extract_block(response_text, "//<fix start>", "//<fix end>") or
                extract_block(response_text, "<fix start>",   "<fix end>")   or
                extract_block(response_text, "<fix start>",   "</fix end>"))
    imports  = (extract_block(response_text, "//<import start>", "//<import end>") or
                extract_block(response_text, "<import start>",   "<import end>"))
    fix_code = strip_blank_lines(fix_code)
    imports  = imports.strip()
    parse_status = determine_parse_status(response_text, fix_code)
    return fix_code, imports, parse_status


# -- patch building (delegates to section4) ------------------------------------

def build_patched_content(original_text, fix_code, victim_method, polluter_method):
    from section4_compilation import build_patched_content as _bpc
    return _bpc(original_text, fix_code, victim_method, polluter_method)


def insert_imports(content, imports_text):
    from section4_compilation import insert_imports as _ii
    return _ii(content, imports_text)


def extract_imports_from_fix_code(fix_code, imports):
    from section4_compilation import extract_imports_from_fix_code as _eifc
    return _eifc(fix_code, imports)


# -- compile + stitch sub-steps ------------------------------------------------

def do_compile(patched_content, test_src_abs, maven_mod_abs, test_src_rel, attempt_dir):
    from section4_compilation import invalidate_class_cache
    write_file(test_src_abs, patched_content)
    invalidate_class_cache(maven_mod_abs, test_src_rel)
    log_path = os.path.join(attempt_dir, "compile.log")
    cmd = f"mvn test-compile -B -o {SKIP_FLAGS}"
    rc, output, elapsed = run_cmd(cmd, maven_mod_abs, COMPILE_TIMEOUT, log_path)
    status = compile_status_from_output(rc, output, test_src_rel)
    return {"status": status, "output": output, "elapsed": elapsed}


def do_stitch(original_prompt, fix_code, imports, compile_errors,
              original_text, victim_meth, polluter_meth,
              test_src_abs, maven_mod_abs, test_src_rel, attempt_dir):
    stitch_prompt = original_prompt + STITCH_PROMPT_SUFFIX.format(
        attempted_fix_code=fix_code,
        attempted_imports=imports if imports.strip() else "(none)",
        compiler_errors=compile_errors,
    )

    result = {
        "ran":                  True,
        "status":               "FAIL",
        "patched_content":      None,
        "fix_code":             fix_code,
        "imports":              imports,
        "last_compile_errors":  compile_errors,
        "compile_elapsed":      0.0,
        "prompt_tokens":        None,
        "completion_tokens":    None,
        "total_tokens":         None,
        "api_elapsed":          None,
        "stitch_prompt":        stitch_prompt,
        "stitch_response":      None,
    }

    try:
        text, s_pt, s_ct, s_tt, s_elapsed = call_gpt4(stitch_prompt)
        result["prompt_tokens"]     = s_pt
        result["completion_tokens"] = s_ct
        result["total_tokens"]      = s_tt
        result["api_elapsed"]       = s_elapsed
        result["stitch_response"]   = text

        s_fix, s_imports, s_parse = parse_gpt4_response(text)

        if s_parse == "OK":
            s_fix, s_imports = extract_imports_from_fix_code(s_fix, s_imports)
            s_base = build_patched_content(original_text, s_fix, victim_meth, polluter_meth)
            if s_base:
                s_content = insert_imports(s_base, s_imports)
                write_file(test_src_abs, s_content)

                from section4_compilation import invalidate_class_cache
                invalidate_class_cache(maven_mod_abs, test_src_rel)
                s_log = os.path.join(attempt_dir, "compile.log")
                cmd   = f"mvn test-compile -B -o {SKIP_FLAGS}"
                rc, out, elapsed_c = run_cmd(cmd, maven_mod_abs, COMPILE_TIMEOUT, s_log)
                result["compile_elapsed"] = elapsed_c

                s_stat = compile_status_from_output(rc, out, test_src_rel)
                result["status"]               = s_stat
                result["fix_code"]             = s_fix
                result["imports"]              = s_imports
                result["last_compile_errors"]  = extract_compiler_errors(out, test_src_rel,
                                                                          source_content=s_content)
                if s_stat == "PASS":
                    result["patched_content"] = s_content
    except Exception as e:
        result["stitch_error"] = str(e)

    return result


# -- per-row retry loop --------------------------------------------------------

def _blank_attempt_metrics():
    return {
        "attempt_num":                  None,
        "source":                       None,
        "compiled":                     False,
        "stitched":                     False,
        "stitch_worked":                False,
        "test_result":                  None,
        "attempt_result":               None,
        "gpt4_prompt_tokens":           None,
        "gpt4_completion_tokens":       None,
        "gpt4_total_tokens":            None,
        "gpt4_api_elapsed_seconds":     None,
        "stitch_prompt_tokens":         None,
        "stitch_completion_tokens":     None,
        "stitch_total_tokens":          None,
        "stitch_api_elapsed_seconds":   None,
        "compile_elapsed_seconds":      None,
        "stitch_compile_elapsed_seconds": None,
        "test_elapsed_seconds":         None,
        "victim_alone_result":          None,
        "victim_alone_elapsed_seconds": None,
        "polluter_alone_result":        None,
        "polluter_alone_elapsed_seconds": None,
    }


def _write_attempt_json(attempt_dir, data):
    with open(os.path.join(attempt_dir, "attempt.json"), "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


def _write_gpt4_file(attempt_dir, filename, prompt, response):
    content = f"=== PROMPT ===\n{prompt}\n\n=== RESPONSE ===\n{response or '(none)'}\n"
    write_file(os.path.join(attempt_dir, filename), content)


def process_row_loop(row_key, target, global_metrics, include_repro=True):
    out = os.path.join(OUT_DIR, row_key)
    os.makedirs(out, exist_ok=True)

    test_src_rel  = target["test_src_path"]
    test_src_abs  = os.path.join(BASE_DIR, test_src_rel)
    maven_mod_abs = os.path.join(BASE_DIR, target["maven_module_path"])
    victim_meth   = target["victim_method"]
    polluter_meth = target["polluter_method"]

    entry           = load_entry_for_row(row_key, target)
    original_prompt = build_original_prompt(entry, include_repro=include_repro)
    original_text   = read_file(test_src_abs)

    env = make_env(target.get("required_java_version", 8))

    s4m      = s4_row_metrics(row_key)
    s4_final = s4m.get("final_compile_status", "NA")

    attempt_metrics  = []
    recent_errors    = []
    final_status     = None

    prev_fix_code   = ""
    prev_imports    = ""
    prev_error      = ""
    prev_error_type = None

    # -- Attempt 1 from section 4 --
    if s4_final == "FAIL":
        fc_path = os.path.join(PARSED_DIR, row_key, "fix_code.java")
        im_path = os.path.join(PARSED_DIR, row_key, "imports.txt")
        prev_fix_code  = read_file(fc_path).strip() if os.path.isfile(fc_path) else ""
        prev_imports   = read_file(im_path).strip()  if os.path.isfile(im_path) else ""
        prev_error     = s4_last_compile_errors(row_key, test_src_rel)
        prev_error_type = "COMPILE"
        recent_errors.append(prev_error)

        am1 = _blank_attempt_metrics()
        am1.update({
            "attempt_num":                  1,
            "source":                       "section4",
            "compiled":                     False,
            "stitched":                     s4m.get("stitched", False),
            "stitch_worked":                False,
            "attempt_result":               "COMPILE_ERROR",
            "stitch_prompt_tokens":         s4m.get("stitch_prompt_tokens"),
            "stitch_completion_tokens":     s4m.get("stitch_completion_tokens"),
            "stitch_total_tokens":          s4m.get("stitch_total_tokens"),
            "stitch_api_elapsed_seconds":   s4m.get("stitch_api_elapsed_seconds"),
            "compile_elapsed_seconds":      s4m.get("compile_elapsed_seconds"),
            "stitch_compile_elapsed_seconds": s4m.get("stitch_compile_elapsed_seconds"),
        })
        attempt_metrics.append(am1)

        a1_dir = os.path.join(out, "attempt1")
        os.makedirs(a1_dir, exist_ok=True)
        _write_attempt_json(a1_dir, {
            "source": "section4",
            "compile_status": "FAIL",
            "attempt_result": "COMPILE_ERROR",
            "fix_code": prev_fix_code,
            "imports": prev_imports,
        })
        start_attempt = 2
    else:
        start_attempt = 1

    # -- Retry loop --
    for attempt_num in range(start_attempt, MAX_ATTEMPTS + 1):
        attempt_dir = os.path.join(out, f"attempt{attempt_num}")
        os.makedirs(attempt_dir, exist_ok=True)

        am = _blank_attempt_metrics()
        am["attempt_num"] = attempt_num

        patched_content = None
        fix_code        = ""
        imports         = ""
        retry_prompt    = None
        retry_response  = None

        try:
            # -- Get this attempt's patch --
            if attempt_num == 1:
                pc, label, fc, im = s4_best_patch(row_key)
                if pc is None:
                    am["attempt_result"] = "COMPILE_ERROR"
                    _write_attempt_json(attempt_dir, {
                        "source": "section4", "compile_status": "NA",
                        "attempt_result": "COMPILE_ERROR",
                    })
                    attempt_metrics.append(am)
                    break

                patched_content = pc
                fix_code        = fc
                imports         = im
                am["source"]    = label
                write_file(os.path.join(attempt_dir, "patched_test.java"), patched_content)

            else:
                am["source"] = "gpt4_retry"

                if prev_error_type == "COMPILE":
                    suffix = RETRY_COMPILE_SUFFIX.format(
                        attempted_fix_code=prev_fix_code,
                        attempted_imports=prev_imports if prev_imports.strip() else "(none)",
                        compiler_errors=prev_error,
                    )
                else:
                    suffix = RETRY_TEST_SUFFIX.format(
                        attempted_fix_code=prev_fix_code,
                        attempted_imports=prev_imports if prev_imports.strip() else "(none)",
                        test_failure_excerpt=prev_error,
                    )

                retry_prompt = original_prompt + suffix

                print(f"        [attempt {attempt_num}] Calling GPT-4...", flush=True)
                text, g_pt, g_ct, g_tt, g_elapsed = call_gpt4(retry_prompt)
                retry_response = text
                am["gpt4_prompt_tokens"]       = g_pt
                am["gpt4_completion_tokens"]   = g_ct
                am["gpt4_total_tokens"]        = g_tt
                am["gpt4_api_elapsed_seconds"] = g_elapsed

                fix_code, imports, parse_status = parse_gpt4_response(text)

                if parse_status != "OK":
                    am["attempt_result"] = "COMPILE_ERROR"
                    _write_attempt_json(attempt_dir, {
                        "source": "gpt4_retry", "compile_status": "FAIL",
                        "attempt_result": "COMPILE_ERROR", "fix_code": fix_code, "imports": imports,
                    })
                    if retry_prompt:
                        _write_gpt4_file(attempt_dir, "gpt4_retry.txt", retry_prompt, retry_response)
                    attempt_metrics.append(am)
                    recent_errors.append(f"PARSE_{parse_status}")
                    if (len(recent_errors) >= SAME_ERROR_LIMIT and
                            len(set(recent_errors[-SAME_ERROR_LIMIT:])) == 1):
                        print(f"        Same error {SAME_ERROR_LIMIT}x -- stopping", flush=True)
                        break
                    continue

                fix_code, imports = extract_imports_from_fix_code(fix_code, imports)
                base = build_patched_content(original_text, fix_code, victim_meth, polluter_meth)
                if base is None:
                    am["attempt_result"] = "COMPILE_ERROR"
                    _write_attempt_json(attempt_dir, {
                        "source": "gpt4_retry", "compile_status": "FAIL",
                        "attempt_result": "COMPILE_ERROR", "fix_code": fix_code, "imports": imports,
                    })
                    if retry_prompt:
                        _write_gpt4_file(attempt_dir, "gpt4_retry.txt", retry_prompt, retry_response)
                    attempt_metrics.append(am)
                    recent_errors.append("METHOD_NOT_FOUND")
                    if (len(recent_errors) >= SAME_ERROR_LIMIT and
                            len(set(recent_errors[-SAME_ERROR_LIMIT:])) == 1):
                        print(f"        Same error {SAME_ERROR_LIMIT}x -- stopping", flush=True)
                        break
                    continue

                patched_content = insert_imports(base, imports)
                write_file(os.path.join(attempt_dir, "patched_test.java"), patched_content)

            # -- Compile --
            print(f"        [attempt {attempt_num}] Compiling...", flush=True)
            c_result = do_compile(patched_content, test_src_abs, maven_mod_abs, test_src_rel, attempt_dir)
            am["compile_elapsed_seconds"] = c_result["elapsed"]

            if c_result["status"] != "PASS":
                # -- Stitch --
                print(f"        [attempt {attempt_num}] Compile FAIL -- stitching...", flush=True)
                compile_errors = extract_compiler_errors(c_result["output"], test_src_rel,
                                                          source_content=patched_content)
                stitch = do_stitch(
                    original_prompt, fix_code, imports, compile_errors,
                    original_text, victim_meth, polluter_meth,
                    test_src_abs, maven_mod_abs, test_src_rel, attempt_dir
                )
                am["stitched"]                       = True
                am["stitch_prompt_tokens"]           = stitch["prompt_tokens"]
                am["stitch_completion_tokens"]       = stitch["completion_tokens"]
                am["stitch_total_tokens"]            = stitch["total_tokens"]
                am["stitch_api_elapsed_seconds"]     = stitch["api_elapsed"]
                am["stitch_compile_elapsed_seconds"] = stitch["compile_elapsed"]

                # Write stitch GPT-4 file
                if stitch.get("stitch_prompt") and stitch.get("stitch_response"):
                    _write_gpt4_file(attempt_dir, "gpt4_stitch.txt",
                                     stitch["stitch_prompt"], stitch["stitch_response"])

                if stitch["status"] == "PASS":
                    am["stitch_worked"]  = True
                    am["compiled"]       = True
                    patched_content      = stitch["patched_content"]
                    fix_code             = stitch["fix_code"]
                    imports              = stitch["imports"]
                    # Overwrite patched_test.java with stitched version
                    write_file(os.path.join(attempt_dir, "patched_test.java"), patched_content)
                    print(f"        [attempt {attempt_num}] Stitch PASS", flush=True)
                else:
                    am["compiled"]       = False
                    am["attempt_result"] = "COMPILE_ERROR"
                    err = stitch["last_compile_errors"] or compile_errors
                    prev_error      = err
                    prev_error_type = "COMPILE"
                    prev_fix_code   = stitch["fix_code"]
                    prev_imports    = stitch["imports"]
                    print(f"        [attempt {attempt_num}] Stitch FAIL", flush=True)
            else:
                am["compiled"] = True

            # -- Run test if compiled --
            if am["compiled"]:
                print(f"        [attempt {attempt_num}] Running OD test...", flush=True)
                test_log = os.path.join(attempt_dir, "test_run.log")
                run_stat, test_output, test_elapsed = run_od_test(target, test_log, env)

                am["test_result"]          = run_stat
                am["test_elapsed_seconds"] = test_elapsed
                print(f"        [attempt {attempt_num}] Test: {run_stat}", flush=True)

                if run_stat == "PASSED":
                    # Isolation checks
                    print(f"        [attempt {attempt_num}] Checking victim alone...", flush=True)
                    va_log = os.path.join(attempt_dir, "victim_alone.log")
                    va_stat, _, va_elapsed = run_victim_alone(target, va_log, env)
                    am["victim_alone_result"]          = va_stat
                    am["victim_alone_elapsed_seconds"] = va_elapsed

                    print(f"        [attempt {attempt_num}] Checking polluter alone...", flush=True)
                    pa_log = os.path.join(attempt_dir, "polluter_alone.log")
                    pa_stat, _, pa_elapsed = run_polluter_alone(target, pa_log, env)
                    am["polluter_alone_result"]          = pa_stat
                    am["polluter_alone_elapsed_seconds"] = pa_elapsed
                    if pa_stat != "PASSED":
                        print(f"        [attempt {attempt_num}] WARNING: polluter alone {pa_stat}", flush=True)

                    if va_stat != "PASSED":
                        # Victim fails alone — fix broke the test
                        print(f"        [attempt {attempt_num}] victim alone FAILED — marking NOT_FIXED", flush=True)
                        am["attempt_result"] = "NOT_FIXED"
                        prev_error      = f"Fix broke victim test: fails in isolation (victim_alone={va_stat})"
                        prev_error_type = "TEST"
                        prev_fix_code   = fix_code
                        prev_imports    = imports
                    else:
                        am["attempt_result"] = "FIXED"
                        final_status         = "FIXED"
                else:
                    am["attempt_result"] = "NOT_FIXED"
                    prev_error           = extract_test_failure_excerpt(test_output)
                    prev_error_type      = "TEST"
                    prev_fix_code        = fix_code
                    prev_imports         = imports

        except Exception as e:
            print(f"        [attempt {attempt_num}] ERROR: {e}", flush=True)
            am["attempt_result"] = "COMPILE_ERROR"
            prev_error      = f"ERROR: {e}"
            prev_error_type = "COMPILE"

        finally:
            restore_source(target)

        # Write consolidated attempt.json
        attempt_data = {
            "source":           am["source"],
            "compile_status":   "PASS" if am["compiled"] else "FAIL",
            "stitch_status":    "PASS" if am.get("stitch_worked") else ("FAIL" if am.get("stitched") else None),
            "test_status":      am.get("test_result"),
            "attempt_result":   am.get("attempt_result") or "COMPILE_ERROR",
            "fix_code":         fix_code,
            "imports":          imports,
            "victim_alone_status":  am.get("victim_alone_result"),
            "polluter_alone_status": am.get("polluter_alone_result"),
        }
        _write_attempt_json(attempt_dir, attempt_data)

        # Write GPT-4 retry file
        if retry_prompt:
            _write_gpt4_file(attempt_dir, "gpt4_retry.txt", retry_prompt, retry_response)

        # Record error for consecutive-same-error check
        if am["attempt_result"] != "FIXED":
            recent_errors.append(prev_error)
            if (len(recent_errors) >= SAME_ERROR_LIMIT and
                    len(set(recent_errors[-SAME_ERROR_LIMIT:])) == 1):
                attempt_metrics.append(am)
                print(f"        Same error {SAME_ERROR_LIMIT}x in a row -- stopping early",
                      flush=True)
                break

        attempt_metrics.append(am)

        if final_status == "FIXED":
            break

    # -- Determine final status --
    if final_status is None:
        if any(a.get("test_result") is not None for a in attempt_metrics):
            final_status = "NOT_FIXED"
        else:
            final_status = "COMPILE_ERROR"

    # Write consolidated row_result.json
    row_result = {
        "final_status": final_status,
        "attempt_metrics": attempt_metrics,
    }
    with open(os.path.join(out, "row_result.json"), "w", encoding="utf-8") as f:
        json.dump(row_result, f, indent=2)

    # Aggregate into global metrics
    stitch_attempts  = sum(1 for a in attempt_metrics if a.get("stitched"))
    stitch_successes = sum(1 for a in attempt_metrics if a.get("stitch_worked"))
    gpt4_tokens      = sum((a.get("gpt4_total_tokens")   or 0) for a in attempt_metrics)
    stitch_tokens    = sum((a.get("stitch_total_tokens") or 0) for a in attempt_metrics)

    global_metrics[row_key] = {
        "final_status":          final_status,
        "total_attempts":        len(attempt_metrics),
        "stitch_attempts":       stitch_attempts,
        "stitch_successes":      stitch_successes,
        "total_gpt4_tokens":     gpt4_tokens,
        "total_stitch_tokens":   stitch_tokens,
        "total_test_elapsed":    round(
            sum((a.get("test_elapsed_seconds") or 0) for a in attempt_metrics), 2
        ),
        "attempts":              attempt_metrics,
    }

    return final_status, attempt_metrics


# -- main ----------------------------------------------------------------------

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


def parse_args():
    parser = argparse.ArgumentParser(
        description="Run OD tests on compiled patches with a retry loop."
    )
    parser.add_argument("--rows", help="Rows to process, e.g. '1,5,10' or '1-10'")
    parser.add_argument("--only", help="Process only this row key, e.g. row01")
    parser.add_argument("--no-repro", action="store_true",
                        help="Read from no-repro dirs and exclude repro steps from prompts.")
    return parser.parse_args()


def main():
    args = parse_args()
    global METRICS_S1, COMP_DIR, PARSED_DIR, OUT_DIR, METRICS_FILE

    if args.no_repro:
        METRICS_S1   = os.path.join(OAI_DIR, "section1_patches_no_repro", "metrics.json")
        COMP_DIR     = os.path.join(OAI_DIR, "section4_compilation_no_repro")
        PARSED_DIR   = os.path.join(OAI_DIR, "section2_parsed_no_repro")
        OUT_DIR      = os.path.join(OAI_DIR, "section5_test_runs_no_repro")
        METRICS_FILE = os.path.join(OUT_DIR, "metrics.json")
    os.makedirs(OUT_DIR, exist_ok=True)

    ensure_openai_api_key()

    with open(METRICS_S1, encoding="utf-8") as f:
        s1_metrics = json.load(f)

    row_keys   = sorted(s1_metrics.keys())
    target_map = load_targets()
    g_metrics  = load_global_metrics()

    # Filter rows
    if args.only:
        if args.only not in row_keys:
            print(f"ERROR: {args.only} not in section1 metrics")
            sys.exit(1)
        row_keys = [args.only]
    elif args.rows:
        requested = parse_rows_arg(args.rows)
        row_keys = [f"row{n:02d}" for n in requested if f"row{n:02d}" in row_keys]

    variant_tag = " [NO-REPRO]" if args.no_repro else ""
    print("=" * 65)
    print(f"SECTION 5: TEST EXECUTION WITH RETRY LOOP{variant_tag}")
    print(f"  MAX_ATTEMPTS={MAX_ATTEMPTS}  SAME_ERROR_LIMIT={SAME_ERROR_LIMIT}")
    print("=" * 65)

    # Restore all sources before starting
    print("\nRestoring all source files to git state...")
    for rk in row_keys:
        row_num = int(rk.replace("row", ""))
        t = target_map.get(row_num)
        if t:
            restore_source(t)

    # Pre-compile: ensure test-classes exist for each repo
    # Avoids full recompilation (e.g. fastjson 2496 files) during per-attempt compiles
    import glob as _glob
    compiled_repos = set()
    for rk in row_keys:
        row_num = int(rk.replace("row", ""))
        t = target_map.get(row_num)
        if not t:
            continue
        maven_mod_abs = os.path.join(BASE_DIR, t["maven_module_path"])
        if maven_mod_abs in compiled_repos:
            continue
        tc_dir = os.path.join(maven_mod_abs, "target", "test-classes")
        has_classes = os.path.isdir(tc_dir) and bool(
            _glob.glob(os.path.join(tc_dir, "**", "*.class"), recursive=True)
        )
        if not has_classes:
            print(f"\nPre-compiling {t['repo_slug']} ({t['package']}) — test-classes missing...")
            env = make_env(t.get("required_java_version", 8))
            mvn = t.get("mvn_cmd", "mvn")
            pre_log = os.path.join(OUT_DIR, f"_precompile_{t['repo_slug']}.log")
            cmd = f"{mvn} test-compile -B {SKIP_FLAGS}"
            rc, _, elapsed = run_cmd(cmd, os.path.join(BASE_DIR, t["repo_local_path"]),
                                     1800, pre_log, env)
            if rc == 0:
                print(f"  Pre-compile OK ({elapsed}s)")
            else:
                print(f"  Pre-compile FAILED ({elapsed}s) — see {pre_log}")
        compiled_repos.add(maven_mod_abs)

    print(f"\nProcessing {len(row_keys)} row(s)...\n")

    results = []
    for row_key in row_keys:
        row_num = int(row_key.replace("row", ""))
        target  = target_map.get(row_num)

        if target is None:
            print(f"  {row_key}: SKIP -- no target metadata")
            continue

        # Skip if section4 didn't process this row
        skip_reason = s4_skip_reason(row_key)
        if skip_reason not in ("OK", "MISSING"):
            print(f"  {row_key}: SKIPPED (section4 skip_reason={skip_reason})")
            os.makedirs(os.path.join(OUT_DIR, row_key), exist_ok=True)
            row_result = {"final_status": "SKIPPED", "attempt_metrics": []}
            with open(os.path.join(OUT_DIR, row_key, "row_result.json"), "w") as f:
                json.dump(row_result, f, indent=2)
            g_metrics[row_key] = {"final_status": "SKIPPED", "total_attempts": 0,
                                   "stitch_attempts": 0, "stitch_successes": 0,
                                   "total_gpt4_tokens": 0, "total_stitch_tokens": 0,
                                   "total_test_elapsed": 0, "attempts": []}
            results.append((row_key, "SKIPPED", g_metrics[row_key]))
            save_global_metrics(g_metrics)
            continue

        s4m      = s4_row_metrics(row_key)
        s4_final = s4m.get("final_compile_status", "NA")
        print(f"  {row_key} ({target['test_id']})  [s4_compile={s4_final}]", flush=True)

        final_status, ams = process_row_loop(row_key, target, g_metrics,
                                              include_repro=not args.no_repro)
        save_global_metrics(g_metrics)

        n_att    = len(ams)
        n_stitch = sum(1 for a in ams if a.get("stitched"))
        n_sw     = sum(1 for a in ams if a.get("stitch_worked"))
        gpt4_tok = sum((a.get("gpt4_total_tokens") or 0) for a in ams)
        print(f"    -> {final_status}  "
              f"(attempts={n_att}, stitches={n_stitch}/{n_sw} worked, "
              f"gpt4_tokens={gpt4_tok})", flush=True)

        results.append((row_key, final_status, g_metrics.get(row_key, {})))

    # -- Summary table --
    print("\n" + "=" * 85)
    print(f"{'row':<8} {'final_status':<16} {'attempts':<10} {'stitches':<10} "
          f"{'stitch_ok':<10} {'gpt4_tok':<10} {'test_sec':<10}")
    print("-" * 85)

    counts = {"FIXED": 0, "NOT_FIXED": 0, "COMPILE_ERROR": 0, "SKIPPED": 0}
    for row_key, fs, rm in results:
        n_att    = rm.get("total_attempts", 0)
        n_stitch = rm.get("stitch_attempts", 0)
        n_sw     = rm.get("stitch_successes", 0)
        gpt4_tok = rm.get("total_gpt4_tokens", 0)
        test_s   = rm.get("total_test_elapsed", 0.0)
        print(f"{row_key:<8} {fs:<16} {n_att:<10} {n_stitch:<10} "
              f"{n_sw:<10} {gpt4_tok:<10} {test_s:<10.1f}")
        counts[fs] = counts.get(fs, 0) + 1

    print("=" * 85)
    print(f"\nFIXED         : {counts.get('FIXED', 0)}")
    print(f"NOT_FIXED     : {counts.get('NOT_FIXED', 0)}")
    print(f"COMPILE_ERROR : {counts.get('COMPILE_ERROR', 0)}")
    print(f"SKIPPED       : {counts.get('SKIPPED', 0)}")

    print("\nSECTION 5 COMPLETE")


if __name__ == "__main__":
    main()
