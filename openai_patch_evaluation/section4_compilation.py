#!/usr/bin/env python3
"""
Section 4: Patch Application, Compilation, and Stitching

For each row with a parsed patch:
  1. Build the patched file: fix_code (method replacement) + imports always inserted.
     WHY single compile instead of strict/relaxed: GPT-4 is explicitly instructed to
     output imports in a separate //<import start> section. Running a "strict" pass
     (without imports) would almost always fail when new imports are needed, burning
     a full Maven compile for no diagnostic value. The strict/relaxed split was
     designed for weaker models that sometimes embedded imports inline — it does not
     apply here.
  2. Run mvn test-compile on the patched file.
  3. If compilation fails, STITCH once:
       - Extract relevant compiler error lines from the failed compile.
       - Re-prompt GPT-4: full original prompt + "previous attempt failed" block
         showing BOTH the attempted fix code AND the imports that were applied,
         so GPT-4 sees the exact state that was compiled and can reason about
         what is still wrong.
       - Parse the stitch response, apply it (fix code + any new imports), recompile.
  4. Restore the original source file unconditionally.

WHY stitch is only applied once:
  A single round catches the common cases (wrong method signature, missing symbol,
  type mismatch). Unbounded retries risk masking fundamental incompatibilities.
  Before/after states are stored separately for analysis.

Output per row:  section4_compilation/row{N}/
  patched_test.java        — patched file (fix code + imports)
  compile.log              — compiler output
  compile_status.txt       — PASS | FAIL | NA
  stitch_prompt.txt        — full stitch prompt sent to GPT-4 (if stitching ran)
  stitch_raw_response.txt  — raw GPT-4 stitch response (if stitching ran)
  stitched_test.java       — stitch-applied patched content (if stitching ran)
  stitch_compile.log       — compiler output after stitching (if stitching ran)
  stitch_compile_status.txt — PASS | FAIL (if stitching ran)
  skip_reason.txt          — OK | PARSE_FAILED | EMPTY_FIX_CODE
                             | METHOD_NOT_FOUND | TEST_SRC_MISSING

Metrics written to section4_compilation/metrics.json:
  row_key -> {
    compile_elapsed_seconds,      # initial compile time
    final_compile_status,         # PASS | FAIL | NA  (best outcome)
    stitched,                     # true | false
    stitch_prompt_tokens,         # null if no stitch
    stitch_completion_tokens,
    stitch_total_tokens,
    stitch_api_elapsed_seconds,
    stitch_compile_elapsed_seconds
  }
"""

import argparse
import json
import os
import re
import signal
import subprocess
import sys
import time

BASE_DIR     = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
OAI_DIR      = os.path.dirname(os.path.abspath(__file__))
METRICS_S1   = os.path.join(OAI_DIR, "section1_patches", "metrics.json")
PARSED_DIR   = os.path.join(OAI_DIR, "section2_parsed")
OUT_DIR      = os.path.join(OAI_DIR, "section4_compilation")
METRICS_FILE = os.path.join(OUT_DIR, "metrics.json")

COMPILE_TIMEOUT = 600   # seconds

SKIP_FLAGS = (
    "-Drat.skip=true "
    "-Dcheckstyle.skip=true "
    "-Ddisable.checks=true "
    "-Denforcer.skip=true "
    "-Dspotbugs.skip=true "
    "-Djacoco.skip=true "
    "-Danimal.sniffer.skip=true "
    "-Dmaven.antrun.skip=true "
    "-Dspotless.check.skip=true "
    "-Dspring-javaformat.skip=true "
    "-Dmaven.test.failure.ignore=true "
    "-Djacoco.agent.argLine= "
    "-DargLine= "
    "-Dmaven.plugin.skip=true "
    "-Dlicense.skip=true "
    "-Dlicense.skipUpdateLicense=true "
    "-DtrimStackTrace=false "
)

JAVA8_HOME = "/Library/Java/JavaVirtualMachines/adoptopenjdk-8.jdk/Contents/Home"

# ── stitch prompt ─────────────────────────────────────────────────────────────
#
# The stitch prompt is the full original section-1 prompt (re-built from the
# same entry fields) with this suffix appended.  GPT-4 sees the complete
# context plus the concrete failure evidence so it can make a targeted fix.

STITCH_PROMPT_SUFFIX = """

--------------------------------------------------------------------------------
PREVIOUS ATTEMPT FAILED TO COMPILE
--------------------------------------------------------------------------------

A fix was generated from the instructions above and applied to the test file,
but it did NOT compile. Below is the exact state that was compiled and the
resulting errors, so you can identify what needs to change.

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
- If the fix requires different or additional imports, include them in //<import start> ... //<import end>.
"""


# ── helpers ───────────────────────────────────────────────────────────────────

def read_file(path):
    try:
        subprocess.run(["xattr", "-c", path], capture_output=True, timeout=5)
    except Exception:
        pass
    try:
        r = subprocess.run(["cat", path], capture_output=True, text=True, timeout=30)
        if r.returncode == 0 and r.stdout:
            return r.stdout
    except Exception:
        pass
    with open(path, encoding="utf-8") as f:
        return f.read()


def make_env():
    env = os.environ.copy()
    env["JAVA_HOME"] = JAVA8_HOME
    env["PATH"] = os.path.join(JAVA8_HOME, "bin") + ":" + env.get("PATH", "")
    return env


def run_compile(maven_module_abs, log_path, offline=False):
    """Run mvn test-compile; return (rc, output, elapsed_seconds)."""
    offline_flag = "-o" if offline else ""
    cmd = f"mvn test-compile -B {offline_flag} {SKIP_FLAGS}"
    env = make_env()
    t0 = time.time()
    try:
        proc = subprocess.Popen(
            cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            cwd=maven_module_abs, env=env, text=True,
            preexec_fn=os.setsid
        )
        try:
            stdout, _ = proc.communicate(timeout=COMPILE_TIMEOUT)
            rc = proc.returncode
        except subprocess.TimeoutExpired:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except Exception:
                proc.kill()
            stdout, _ = proc.communicate()
            stdout += f"\nTIMEOUT after {COMPILE_TIMEOUT}s\n"
            rc = 1
    except Exception as e:
        stdout = f"ERROR launching command: {e}\n"
        rc = 1
    elapsed = round(time.time() - t0, 2)
    with open(log_path, "w", encoding="utf-8") as f:
        f.write(stdout)
    return rc, stdout, elapsed


def compile_status_from_output(rc, output, test_src_rel):
    if rc != 0:
        return "FAIL"
    filename = os.path.basename(test_src_rel)
    for line in output.splitlines():
        if "[ERROR]" in line and filename in line:
            return "FAIL"
    return "PASS"


def invalidate_class_cache(maven_module_abs, test_src_rel):
    """Delete .class files for the test being patched to force Maven recompilation."""
    for marker in ("src/test/java/", "src/test/"):
        idx = test_src_rel.find(marker)
        if idx >= 0:
            rel_class_path = test_src_rel[idx + len(marker):]
            break
    else:
        return
    rel_class_path = rel_class_path.replace(".java", "")
    class_dir = os.path.join(maven_module_abs, "target", "test-classes",
                              os.path.dirname(rel_class_path))
    class_base = os.path.basename(rel_class_path)
    if os.path.isdir(class_dir):
        for f in os.listdir(class_dir):
            if f == class_base + ".class" or f.startswith(class_base + "$"):
                try:
                    os.remove(os.path.join(class_dir, f))
                except OSError:
                    pass


def extract_compiler_errors(output, test_src_rel, max_errors=15, source_content=None):
    """
    Extract only the actual javac error lines from Maven compile output,
    stripping absolute paths and all Maven boilerplate ([INFO], [WARNING],
    plugin messages, etc.).

    When source_content is provided, also shows the source line that failed.

    Produces lines like:
      HttpRequestTest.java:[3480] error: cannot find symbol
        → HttpRequest.setConnectionFactory(HttpRequest.DEFAULT_CONNECTION_FACTORY);
    """
    filename  = os.path.basename(test_src_rel)
    error_re  = re.compile(
        r'\[ERROR\]\s+\S+?' + re.escape(filename) + r':\[(\d+),\d+\]\s+(error:.+)'
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
                    entry += f"\n  → {src_lines[line_num - 1].strip()}"
                results.append(entry)
            if len(results) >= max_errors:
                break
    if not results:
        # Fallback: any [ERROR] line that looks like a compiler message
        skip = ("execute goal", "-> [Help", "stack trace", "Re-run Maven",
                "re-run Maven", "For more information", "[Help 1]", "COMPILATION ERROR")
        for line in output.splitlines():
            s = line.strip()
            if (s.startswith("[ERROR]") and "error:" in s.lower()
                    and not any(p in s for p in skip)):
                results.append(re.sub(r'^\[ERROR\]\s*', '', s))
    return "\n".join(results)


# ── method substitution (mirrors patch_evaluation/section4_compilation.py) ────

def find_method_in_source(lines, method_name):
    sig_pattern = re.compile(
        r'^\s*(public|protected|private)?\s*(static\s+)?void\s+' +
        re.escape(method_name) + r'\s*\('
    )
    for i, line in enumerate(lines):
        if sig_pattern.search(line):
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


def extract_method_text(lines, method_name):
    start, end = find_method_in_source(lines, method_name)
    if start is None:
        return ""
    return "".join(lines[start:end + 1])


def find_all_methods(lines):
    """Return list of (method_name, ann_start, end_idx) for all top-level void methods."""
    sig_pat = re.compile(
        r'^\s*(?:public|protected|private)?\s*(?:static\s+)?void\s+(\w+)\s*\('
    )
    methods = []
    i = 0
    while i < len(lines):
        m = sig_pat.search(lines[i])
        if m:
            method_name = m.group(1)
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
            methods.append((method_name, ann_start, end_idx))
            i = end_idx + 1
        else:
            i += 1
    return methods


def find_method_fuzzy(fix_lines, method_name):
    import difflib
    text = extract_method_text(fix_lines, method_name)
    if text:
        return method_name, text
    sig_pat = re.compile(
        r'^\s*(?:public|protected|private)?\s*(?:static\s+)?void\s+(\w+)\s*\('
    )
    candidates = [m.group(1) for ln in fix_lines for m in [sig_pat.search(ln)] if m]
    if not candidates:
        return None, ""
    close = difflib.get_close_matches(method_name, candidates, n=1, cutoff=0.85)
    if not close:
        return None, ""
    found = close[0]
    text = extract_method_text(fix_lines, found)
    if not text:
        return None, ""
    text = text.replace(found, method_name, 1)
    return found, text


def build_patched_content(original_text, fix_code, victim_method, polluter_method):
    """
    Replace victim (and optionally polluter) method in original_text.
    Also handles extra methods in fix_code: replaces existing ones by name,
    inserts new ones after the victim method.
    Returns patched content string, or None if victim method not found.
    """
    orig_lines = original_text.splitlines(keepends=True)
    fix_lines  = fix_code.splitlines(keepends=True)

    _, patched_victim = find_method_fuzzy(fix_lines, victim_method)
    if not patched_victim:
        # Polluter-only fix fallback
        if polluter_method and polluter_method != victim_method:
            patched_polluter = extract_method_text(fix_lines, polluter_method)
            if patched_polluter:
                p_start, p_end = find_method_in_source(orig_lines, polluter_method)
                if p_start is not None:
                    result = list(orig_lines)
                    new_lines = patched_polluter.splitlines(keepends=True)
                    if not new_lines[-1].endswith("\n"):
                        new_lines[-1] += "\n"
                    result[p_start:p_end + 1] = new_lines
                    return "".join(result)
        return None

    v_start, v_end = find_method_in_source(orig_lines, victim_method)
    if v_start is None:
        return None

    replacements = [(v_start, v_end, patched_victim)]
    new_methods_to_insert = []

    if polluter_method and polluter_method != victim_method:
        patched_polluter = extract_method_text(fix_lines, polluter_method)
        if patched_polluter:
            p_start, p_end = find_method_in_source(orig_lines, polluter_method)
            if p_start is not None:
                replacements.append((p_start, p_end, patched_polluter))

    # Handle extra methods in fix_code (e.g., @Before, @After, utilities)
    handled = {victim_method}
    if polluter_method:
        handled.add(polluter_method)

    all_fix_methods = find_all_methods(fix_lines)
    for mname, m_start, m_end in all_fix_methods:
        if mname in handled:
            continue
        handled.add(mname)
        method_text = "".join(fix_lines[m_start:m_end + 1])

        orig_start, orig_end = find_method_in_source(orig_lines, mname)
        if orig_start is not None:
            # Method exists in original -> replace it
            replacements.append((orig_start, orig_end, method_text))
        else:
            # New method -> collect for insertion
            new_methods_to_insert.append(method_text)

    replacements.sort(key=lambda x: x[0], reverse=True)
    result = list(orig_lines)
    for start, end, new_text in replacements:
        new_lines = new_text.splitlines(keepends=True)
        if not new_lines[-1].endswith("\n"):
            new_lines[-1] += "\n"
        result[start:end + 1] = new_lines

    # Insert new methods after the victim method in the result
    if new_methods_to_insert:
        _, new_v_end = find_method_in_source(result, victim_method)
        if new_v_end is not None:
            insert_pos = new_v_end + 1
            for method_text in new_methods_to_insert:
                insert_lines = ["\n"] + method_text.splitlines(keepends=True)
                if not insert_lines[-1].endswith("\n"):
                    insert_lines[-1] += "\n"
                result[insert_pos:insert_pos] = insert_lines
                insert_pos += len(insert_lines)

    return "".join(result)


def insert_imports(content, new_imports_text):
    if not new_imports_text.strip():
        return content
    new_imps = [
        l.strip() for l in new_imports_text.splitlines()
        if l.strip().startswith("import ")
    ]
    if not new_imps:
        return content
    lines = content.splitlines(keepends=True)
    existing = {l.strip() for l in lines if l.strip().startswith("import ")}
    to_add = [imp for imp in new_imps if imp not in existing]
    if not to_add:
        return content
    last_import_idx = package_idx = None
    for i, line in enumerate(lines):
        s = line.strip()
        if s.startswith("import "):
            last_import_idx = i
        elif s.startswith("package "):
            package_idx = i
    insert_after = last_import_idx if last_import_idx is not None else package_idx
    if insert_after is None:
        insert_after = 0
    insert_lines = [imp + "\n" for imp in to_add]
    result = lines[:insert_after + 1] + insert_lines + lines[insert_after + 1:]
    return "".join(result)


# ── GPT-4 stitch call ─────────────────────────────────────────────────────────

def load_entry_for_row(row_key, target):
    """Load the flaky_test_data entry for this row, matched by victim name."""
    from test_execution import load_entry_for_target
    return load_entry_for_target(target)


def build_original_prompt(entry, include_repro=True):
    """Reconstruct the original section-1 prompt from an entry dict."""
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


def call_gpt4_stitch(full_stitch_prompt):
    """
    Send the stitch prompt to GPT-4 and return
    (response_text, prompt_tokens, completion_tokens, total_tokens, elapsed_seconds).
    """
    import os as _os
    from openai import OpenAI

    api_key = _os.environ.get("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY not set")

    client = OpenAI(api_key=api_key)
    t0 = time.time()
    resp = client.chat.completions.create(
        model="gpt-4",
        temperature=0.2,
        messages=[{"role": "user", "content": full_stitch_prompt}],
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


def parse_stitch_response(response_text):
    """
    Extract fix_code and imports from the stitch GPT-4 response.
    Uses the same extraction logic as section2.
    """
    from section2_parse_patches import (
        extract_block, strip_blank_lines, determine_parse_status
    )

    fix_code = (extract_block(response_text, "//<fix start>", "//<fix end>") or
                extract_block(response_text, "<fix start>",   "<fix end>")   or
                extract_block(response_text, "<fix start>",   "</fix end>"))
    imports  = (extract_block(response_text, "//<import start>", "//<import end>") or
                extract_block(response_text, "<import start>",   "<import end>"))
    pom_snippet = (
        extract_block(response_text, "<!-- <pom.xml start> -->", "<!-- <pom.xml end> -->") or
        extract_block(response_text, "<pom.xml start>", "<pom.xml end>")
    )

    fix_code = strip_blank_lines(fix_code)
    imports  = imports.strip()
    parse_status = determine_parse_status(response_text, fix_code)
    return fix_code, imports, pom_snippet, parse_status


# ── metrics ───────────────────────────────────────────────────────────────────

def load_metrics():
    if os.path.isfile(METRICS_FILE):
        with open(METRICS_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_metrics(metrics):
    with open(METRICS_FILE, "w", encoding="utf-8") as f:
        json.dump(metrics, f, indent=2)


# ── targets ───────────────────────────────────────────────────────────────────

def load_targets():
    from test_execution import load_targets as _lt
    return _lt()


def read_parsed(row_key, filename):
    path = os.path.join(PARSED_DIR, row_key, filename)
    if not os.path.isfile(path):
        return ""
    return read_file(path).strip()


# ── skip helpers ──────────────────────────────────────────────────────────────

def write_skipped(row_key, reason, out):
    os.makedirs(out, exist_ok=True)
    for name, val in [("compile_status.txt", "NA"),
                      ("skip_reason.txt", reason)]:
        with open(os.path.join(out, name), "w") as f:
            f.write(val + "\n")


# ── per-row processing ────────────────────────────────────────────────────────

def process_row(row_key, target, m, include_repro=True):
    """m: row-level metrics dict, modified in place."""
    out = os.path.join(OUT_DIR, row_key)
    os.makedirs(out, exist_ok=True)

    parse_status = read_parsed(row_key, "parse_status.txt")

    # Task 4.1 — skip checks
    if parse_status != "OK":
        write_skipped(row_key, "PARSE_FAILED", out)
        return "NA", "PARSE_FAILED", False

    fix_code  = read_parsed(row_key, "fix_code.java")
    imports   = read_parsed(row_key, "imports.txt")
    pom_snip  = read_parsed(row_key, "pom_snippet.xml")

    if not fix_code.strip():
        write_skipped(row_key, "EMPTY_FIX_CODE", out)
        return "NA", "EMPTY_FIX_CODE", False

    test_src_rel  = target["test_src_path"]
    test_src_abs  = os.path.join(BASE_DIR, test_src_rel)
    maven_mod_abs = os.path.join(BASE_DIR, target["maven_module_path"])
    victim_meth   = target["victim_method"]
    polluter_meth = target["polluter_method"]

    from test_execution import offline_flag_for
    use_offline = bool(offline_flag_for(target.get("repo_slug", "")))

    if not os.path.isfile(test_src_abs):
        write_skipped(row_key, "TEST_SRC_MISSING", out)
        return "NA", "TEST_SRC_MISSING", False

    original_text = read_file(test_src_abs)

    # Task 4.2 — build patched content: fix code + imports always applied together
    base_content = build_patched_content(original_text, fix_code, victim_meth, polluter_meth)
    if base_content is None:
        write_skipped(row_key, "METHOD_NOT_FOUND", out)
        return "NA", "METHOD_NOT_FOUND", False

    patched_content = insert_imports(base_content, imports)

    with open(os.path.join(out, "patched_test.java"), "w", encoding="utf-8") as f:
        f.write(patched_content)

    pom_has_xml = "<dependency>" in pom_snip or "<groupId>" in pom_snip

    stitched = False
    compile_stat = "FAIL"

    try:
        # Task 4.3 — single compile attempt (fix code + imports)
        compile_log = os.path.join(out, "compile.log")
        with open(test_src_abs, "w", encoding="utf-8") as f:
            f.write(patched_content)
        invalidate_class_cache(maven_mod_abs, test_src_rel)
        rc, output, elapsed = run_compile(maven_mod_abs, compile_log, offline=use_offline)
        compile_stat = compile_status_from_output(rc, output, test_src_rel)
        m["compile_elapsed_seconds"] = elapsed

        if pom_has_xml:
            with open(compile_log, "a", encoding="utf-8") as f:
                f.write("\nWARNING: patch declares pom dependency — manual review required\n")

        with open(os.path.join(out, "compile_status.txt"), "w") as f:
            f.write(compile_stat + "\n")

        # Task 4.4 — stitch if compile FAILED
        if compile_stat == "FAIL":
            print(f"    Compile FAILED — running stitch ...", flush=True)

            compiler_errors = extract_compiler_errors(output, test_src_rel,
                                                      source_content=patched_content)

            # Build stitch prompt: original prompt + failure context.
            # Show BOTH fix code and imports so GPT-4 sees the exact compiled state.
            entry = load_entry_for_row(row_key, target)
            original_prompt = build_original_prompt(entry, include_repro=include_repro)
            stitch_prompt = original_prompt + STITCH_PROMPT_SUFFIX.format(
                attempted_fix_code=fix_code,
                attempted_imports=imports if imports.strip() else "(none)",
                compiler_errors=compiler_errors,
            )

            with open(os.path.join(out, "stitch_prompt.txt"), "w", encoding="utf-8") as f:
                f.write(stitch_prompt)

            try:
                (stitch_response, s_pt, s_ct, s_tt, s_elapsed) = call_gpt4_stitch(stitch_prompt)
                m["stitch_prompt_tokens"]       = s_pt
                m["stitch_completion_tokens"]   = s_ct
                m["stitch_total_tokens"]        = s_tt
                m["stitch_api_elapsed_seconds"] = s_elapsed

                with open(os.path.join(out, "stitch_raw_response.txt"), "w", encoding="utf-8") as f:
                    f.write(stitch_response)

                stitch_fix, stitch_imports, _, stitch_parse = parse_stitch_response(stitch_response)

                if stitch_parse == "OK":
                    stitch_base = build_patched_content(
                        original_text, stitch_fix, victim_meth, polluter_meth
                    )
                    if stitch_base:
                        stitched_content = insert_imports(stitch_base, stitch_imports)
                        with open(os.path.join(out, "stitched_test.java"), "w", encoding="utf-8") as f:
                            f.write(stitched_content)

                        s_log = os.path.join(out, "stitch_compile.log")
                        with open(test_src_abs, "w", encoding="utf-8") as f:
                            f.write(stitched_content)
                        invalidate_class_cache(maven_mod_abs, test_src_rel)
                        s_rc, s_output, s_elapsed_c = run_compile(maven_mod_abs, s_log, offline=use_offline)
                        s_stat = compile_status_from_output(s_rc, s_output, test_src_rel)
                        m["stitch_compile_elapsed_seconds"] = s_elapsed_c

                        with open(os.path.join(out, "stitch_compile_status.txt"), "w") as f:
                            f.write(s_stat + "\n")

                        stitched = True
                        print(f"    Stitch compile: {s_stat}", flush=True)
                    else:
                        print(f"    Stitch parse OK but method not found in output", flush=True)
                        m["stitch_compile_elapsed_seconds"] = None
                else:
                    print(f"    Stitch parse status: {stitch_parse}", flush=True)
                    m["stitch_compile_elapsed_seconds"] = None

            except Exception as e:
                print(f"    Stitch ERROR: {e}", flush=True)
                for k in ("stitch_prompt_tokens", "stitch_completion_tokens",
                          "stitch_total_tokens", "stitch_api_elapsed_seconds",
                          "stitch_compile_elapsed_seconds"):
                    m[k] = None

        else:
            for k in ("stitch_prompt_tokens", "stitch_completion_tokens",
                      "stitch_total_tokens", "stitch_api_elapsed_seconds",
                      "stitch_compile_elapsed_seconds"):
                m[k] = None

    finally:
        with open(test_src_abs, "w", encoding="utf-8") as f:
            f.write(original_text)

    # Best outcome: initial compile or stitch compile
    stitch_stat = "NA"
    sp = os.path.join(out, "stitch_compile_status.txt")
    if os.path.isfile(sp):
        stitch_stat = read_file(sp).strip()

    final = "PASS" if compile_stat == "PASS" or stitch_stat == "PASS" else \
            "FAIL" if compile_stat == "FAIL" else "NA"

    m["final_compile_status"] = final
    m["stitched"] = stitched

    with open(os.path.join(out, "skip_reason.txt"), "w") as f:
        f.write("OK\n")

    return compile_stat, "OK", stitched


# ── main ──────────────────────────────────────────────────────────────────────

def parse_args():
    parser = argparse.ArgumentParser(description="Apply patches, compile, and stitch if needed.")
    parser.add_argument("--only", help="Process only this row key, e.g. row01")
    parser.add_argument("--ablation", action="store_true",
                        help="Read from ablation dirs (ablation study).")
    return parser.parse_args()


def main():
    args = parse_args()
    global METRICS_S1, PARSED_DIR, OUT_DIR, METRICS_FILE
    if args.ablation:
        METRICS_S1   = os.path.join(OAI_DIR, "section1_patches_ablation", "metrics.json")
        PARSED_DIR   = os.path.join(OAI_DIR, "section2_parsed_ablation")
        OUT_DIR      = os.path.join(OAI_DIR, "section4_compilation_ablation")
        METRICS_FILE = os.path.join(OUT_DIR, "metrics.json")
    os.makedirs(OUT_DIR, exist_ok=True)

    from test_execution import ensure_openai_api_key
    ensure_openai_api_key()

    with open(METRICS_S1, encoding="utf-8") as f:
        s1_metrics = json.load(f)

    row_keys   = sorted(s1_metrics.keys())
    target_map = load_targets()
    metrics    = load_metrics()

    if args.only:
        if args.only not in row_keys:
            print(f"ERROR: {args.only} not in section1 metrics")
            sys.exit(1)
        row_keys = [args.only]

    variant_tag = " [ABLATION]" if args.ablation else ""
    print("=" * 60)
    print(f"SECTION 4: COMPILATION + STITCHING{variant_tag}")
    print("=" * 60)
    print(f"Processing {len(row_keys)} row(s)...\n")

    # Pre-compile: ensure test-classes exist for each repo (avoids cold-cache slowdowns)
    import glob as _glob
    compiled_repos = set()
    for rk in row_keys:
        row_num_pre = int(rk.replace("row", ""))
        t = target_map.get(row_num_pre)
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
            print(f"\nPre-compiling {t['repo_slug']} — test-classes missing...", flush=True)
            pre_log = os.path.join(OUT_DIR, f"_precompile_{t['repo_slug']}.log")
            rc, _, elapsed_pre = run_compile(
                os.path.join(BASE_DIR, t["repo_local_path"]), pre_log, offline=False
            )
            if rc == 0:
                print(f"  Pre-compile OK ({elapsed_pre}s)")
            else:
                print(f"  Pre-compile FAILED ({elapsed_pre}s) — see {pre_log}")
        compiled_repos.add(maven_mod_abs)

    results = []
    for row_key in row_keys:
        row_num = int(row_key.replace("row", ""))
        target  = target_map.get(row_num)

        if target is None:
            print(f"  {row_key}: SKIP — no target metadata for row_num={row_num}")
            continue

        # Skip if already fully processed
        status_path = os.path.join(OUT_DIR, row_key, "compile_status.txt")
        if os.path.isfile(status_path):
            cstat  = read_file(status_path).strip()
            skip_p = os.path.join(OUT_DIR, row_key, "skip_reason.txt")
            reason = read_file(skip_p).strip() if os.path.isfile(skip_p) else "OK"
            print(f"  {row_key}: SKIP (compile={cstat} reason={reason})")
            results.append((row_key, cstat, reason, False))
            continue

        print(f"  {row_key} ({target['test_id']}) ...", flush=True)
        m = metrics.get(row_key, {})
        compile_stat, reason, stitched = process_row(row_key, target, m,
                                                      include_repro=not args.ablation)
        metrics[row_key] = m
        save_metrics(metrics)

        label = f"compile={compile_stat}" if reason == "OK" else f"SKIP({reason})"
        stitch_tag = " [STITCHED]" if stitched else ""
        print(f"    {label}{stitch_tag}", flush=True)
        results.append((row_key, compile_stat, reason, stitched))

    # Summary
    print("\n" + "-" * 55)
    print(f"{'row_key':<8} {'compile':<8} {'stitched':<10} reason")
    print("-" * 55)
    counts = {"PASS": 0, "PASS_stitch": 0, "FAIL": 0, "NA": 0}
    for row_key, cstat, reason, stitched in results:
        stag = "YES" if stitched else "-"
        print(f"{row_key:<8} {cstat:<8} {stag:<10} {reason}")
        final = metrics.get(row_key, {}).get("final_compile_status", "NA")
        if final == "PASS":
            counts["PASS_stitch" if stitched else "PASS"] += 1
        elif final == "FAIL":
            counts["FAIL"] += 1
        else:
            counts["NA"] += 1

    print(f"\nPASS (initial)       : {counts['PASS']}")
    print(f"PASS (after stitch)  : {counts['PASS_stitch']}")
    print(f"FAIL (all attempts)  : {counts['FAIL']}")
    print(f"NA (skipped)         : {counts['NA']}")

    print("\nSECTION 4 COMPLETE")


if __name__ == "__main__":
    main()
