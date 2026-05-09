#!/usr/bin/env python3
"""
validate_patches.py — Validate existing GPT-4 patches WITHOUT an OpenAI API key.

Finds the most recent patch for each row (from section5 attempts, section4
compilation, or section2 parsed data), compiles it, runs the OD test, and
produces categorization + CSV results — all without making any GPT-4 API calls.

Output directories (never overwrites existing pipeline outputs):
  validation_test_runs/              with_repro validation results
  validation_test_runs_no_repro/     no_repro validation results
  validation_categories/             categorization (both conditions)
  validation_results/                results.csv, summary.csv, skip_log.csv

Usage:
  python3.9 validate_patches.py                      # all rows, both conditions
  python3.9 validate_patches.py --rows 1 2 3-5       # specific rows
  python3.9 validate_patches.py --skip-preflight      # skip repo clone/build check
"""

import argparse
import csv
import glob as _glob
import json
import os
import subprocess
import sys
import time

# ── Path setup ────────────────────────────────────────────────────────────────

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))       # no_api_validation/
BASE_DIR   = os.path.dirname(SCRIPT_DIR)                      # repository root
OAI_DIR    = os.path.join(BASE_DIR, "openai_patch_evaluation")

sys.path.insert(0, OAI_DIR)
sys.path.insert(0, os.path.join(BASE_DIR, "failure_data_collection"))

from test_execution import (
    make_env,
    run_cmd,
    run_od_test,
    run_victim_alone,
    run_polluter_alone,
    SKIP_FLAGS,
    COMPILE_TIMEOUT,
    strategy_for,
    test_source_root_for,
    offline_flag_for,
)
from section4_compilation import (
    invalidate_class_cache,
    build_patched_content,
    insert_imports,
    extract_imports_from_fix_code,
    compile_status_from_output,
)
from section5_test_runs import restore_source
from section6_categorize import categorize

# ── Output directories ───────────────────────────────────────────────────────

VAL_S5_DIR          = os.path.join(SCRIPT_DIR, "validation_test_runs")
VAL_S5_NO_REPRO_DIR = os.path.join(SCRIPT_DIR, "validation_test_runs_no_repro")
VAL_S6_DIR          = os.path.join(SCRIPT_DIR, "validation_categories")
VAL_S7_DIR          = os.path.join(SCRIPT_DIR, "validation_results")

# ── Condition → directory mapping ─────────────────────────────────────────────

CONDITIONS = {
    "with_repro": {
        "s1_dir":      os.path.join(OAI_DIR, "section1_patches"),
        "s2_dir":      os.path.join(OAI_DIR, "section2_parsed"),
        "s4_dir":      os.path.join(OAI_DIR, "section4_compilation"),
        "s5_dir":      os.path.join(OAI_DIR, "section5_test_runs"),
        "val_s5_dir":  VAL_S5_DIR,
    },
    "no_repro": {
        "s1_dir":      os.path.join(OAI_DIR, "section1_patches_no_repro"),
        "s2_dir":      os.path.join(OAI_DIR, "section2_parsed_no_repro"),
        "s4_dir":      os.path.join(OAI_DIR, "section4_compilation_no_repro"),
        "s5_dir":      os.path.join(OAI_DIR, "section5_test_runs_no_repro"),
        "val_s5_dir":  VAL_S5_NO_REPRO_DIR,
    },
}


# ── Helpers ───────────────────────────────────────────────────────────────────

def read_file(path):
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except Exception:
        return ""


def write_file(path, content):
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


def load_json(path):
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def parse_rows_arg(row_args):
    """Parse row specifiers like ['1', '2', '3-5', '10'] into sorted ints."""
    rows = set()
    for arg in row_args:
        if "-" in arg:
            lo, hi = arg.split("-", 1)
            rows.update(range(int(lo), int(hi) + 1))
        else:
            rows.add(int(arg))
    return sorted(rows)


# ── Target loading from CSV ──────────────────────────────────────────────────

CSV_PATH  = os.path.join(BASE_DIR, "data", "final_OD_flaky_tests.csv")
REPOS_DIR = os.path.join(BASE_DIR, "repos")


def _parse_csv_rows(csv_path):
    """Parse the flaky tests CSV into a list of row dicts (all 50 rows)."""
    import csv as _csv
    with open(csv_path, newline="", encoding="utf-8") as f:
        reader = _csv.reader(f)
        header = [h.strip().lower() for h in next(reader)]
        col = {c: header.index(c) for c in
               ["repo name", "commit hash", "package", "victim", "polluter"]}

        rows = []
        for csv_row_num, row in enumerate(reader, start=2):
            row_num = csv_row_num - 1
            repo = row[col["repo name"]].strip()
            if not repo:
                continue
            victim = row[col["victim"]].strip()
            polluter = row[col["polluter"]].strip()
            v_parts = victim.rsplit(".", 1)
            p_parts = polluter.rsplit(".", 1)
            if len(v_parts) != 2 or len(p_parts) != 2:
                continue
            rows.append({
                "row_num":        row_num,
                "repo_url":       repo,
                "commit_hash":    row[col["commit hash"]].strip(),
                "package":        row[col["package"]].strip(),
                "victim":         victim,
                "polluter":       polluter,
                "victim_class":   v_parts[0],
                "victim_method":  v_parts[1],
                "polluter_class": p_parts[0],
                "polluter_method":p_parts[1],
                "same_class":     v_parts[0] == p_parts[0],
            })
    return rows


def _resolve_clone_dir(repo_url, commit_hash, all_rows):
    """Determine the clone directory for a repo, using the same logic as step1."""
    slug = repo_url.rstrip("/").split("/")[-1]
    # Check if this repo has multiple commits in the dataset
    commits = set(r["commit_hash"] for r in all_rows if r["repo_url"] == repo_url)
    if len(commits) > 1:
        return os.path.join(REPOS_DIR, f"{slug}-{commit_hash[:8]}"), slug
    return os.path.join(REPOS_DIR, slug), slug


def load_targets_from_csv():
    """Build target metadata for all 50 rows directly from the CSV.

    Uses relative paths under BASE_DIR/repos/ so the script works regardless
    of where the project directory is located.
    """
    all_rows = _parse_csv_rows(CSV_PATH)

    target_map = {}
    for row in all_rows:
        row_num      = row["row_num"]
        repo_url     = row["repo_url"]
        commit_hash  = row["commit_hash"]
        package      = row["package"]
        victim_class = row["victim_class"]
        same_class   = row["same_class"]

        clone_dir, repo_slug = _resolve_clone_dir(repo_url, commit_hash, all_rows)

        repo_local_path = os.path.relpath(clone_dir, BASE_DIR)

        # Maven module path
        if package not in (".", ""):
            maven_module_abs = os.path.join(clone_dir, package)
        else:
            maven_module_abs = clone_dir
        maven_module_path = os.path.relpath(maven_module_abs, BASE_DIR)

        # Test source path
        class_rel    = victim_class.replace(".", os.sep) + ".java"
        test_src_dir = test_source_root_for(repo_slug)
        if package not in (".", ""):
            test_src_abs = os.path.join(clone_dir, package, test_src_dir, class_rel)
        else:
            test_src_abs = os.path.join(clone_dir, test_src_dir, class_rel)
        test_src_path = os.path.relpath(test_src_abs, BASE_DIR)

        target_map[row_num] = {
            "test_id":                f"row{row_num:02d}_{repo_slug}",
            "row_num":                row_num,
            "repo_url":               repo_url,
            "repo_slug":              repo_slug,
            "commit":                 commit_hash,
            "package":                package,
            "victim":                 row["victim"],
            "victim_class":           victim_class,
            "victim_method":          row["victim_method"],
            "polluter":               row["polluter"],
            "polluter_class":         row["polluter_class"],
            "polluter_method":        row["polluter_method"],
            "same_class":             same_class,
            "repo_local_path":        repo_local_path,
            "test_src_path":          test_src_path,
            "test_src_found":         os.path.isfile(test_src_abs),
            "maven_module_path":      maven_module_path,
            "reproduction_strategy":  strategy_for(repo_slug, same_class),
            "clone_dir":              clone_dir,
            "mvn_cmd":                "mvn",
            "required_java_version":  8,  # updated during repo check
        }

    return target_map


# ── Phase 1: Pre-flight checks ───────────────────────────────────────────────

def preflight_api_check():
    api_key = os.environ.get("OPENAI_API_KEY")
    if api_key:
        print("INFO: OPENAI_API_KEY found but will NOT be used.")
        print("      This is validation-only mode — no GPT-4 calls will be made.")
    else:
        print("INFO: No OPENAI_API_KEY set. This is expected for validation-only mode.")
    print()


def _detect_java_version(clone_dir):
    """Detect Java version from repo files. Returns (version_int, assumed_bool)."""
    import re as _re

    def norm(v):
        v = str(v).strip().strip('"').strip("'")
        m = _re.search(r'(\d+)', v)
        if not m:
            return None
        major = int(m.group(1))
        if major == 1:
            m2 = _re.search(r'1\.(\d+)', v)
            if m2:
                major = int(m2.group(1))
        if major in (7, 8, 11, 17, 21):
            return major
        if major <= 8:
            return 8
        if major <= 11:
            return 11
        if major <= 17:
            return 17
        return 21

    # .sdkmanrc
    p = os.path.join(clone_dir, ".sdkmanrc")
    if os.path.isfile(p):
        for line in open(p):
            m = _re.search(r'java\s*=\s*(\S+)', line)
            if m:
                v = norm(m.group(1))
                if v:
                    return v, False

    # .java-version
    p = os.path.join(clone_dir, ".java-version")
    if os.path.isfile(p):
        v = norm(open(p).read())
        if v:
            return v, False

    # .travis.yml
    p = os.path.join(clone_dir, ".travis.yml")
    if os.path.isfile(p):
        for line in open(p, errors="replace"):
            m = _re.search(r'jdk\s*:\s*(\S+)', line)
            if m:
                v = norm(m.group(1))
                if v:
                    return v, False

    # .github/workflows/*.yml
    for wf in _glob.glob(os.path.join(clone_dir, ".github", "workflows", "*.yml")):
        for line in open(wf, errors="replace"):
            m = _re.search(r'java-version\s*:\s*[\'"]?(\d[\d.]*)', line)
            if m:
                v = norm(m.group(1))
                if v:
                    return v, False

    # pom.xml
    root_pom = os.path.join(clone_dir, "pom.xml")
    if os.path.isfile(root_pom):
        content = open(root_pom, errors="replace").read()
        for tag in ("maven.compiler.source", "java.version", "maven.compiler.release"):
            m = _re.search(rf'<{tag}>\s*([^<]+)\s*</{tag}>', content)
            if m:
                v = norm(m.group(1))
                if v:
                    return v, False

    return 8, True


def _detect_mvn_cmd(clone_dir):
    """Check for Maven wrapper, return 'mvn' or './mvnw'."""
    mvnw = os.path.join(clone_dir, "mvnw")
    if os.path.isfile(mvnw) and os.access(mvnw, os.X_OK):
        return "./mvnw"
    return "mvn"


def _run_shell(cmd, cwd=None, timeout=600):
    r = subprocess.run(cmd, shell=True, capture_output=True, text=True,
                       cwd=cwd, timeout=timeout)
    return r.returncode, r.stdout.strip(), r.stderr.strip()


def preflight_repo_check(target_map, selected_rows):
    """Verify repos are cloned, at the right commit, and built.

    Clones/checks out missing repos automatically. Updates target_map in place
    with detected Java version and mvn command.

    Returns set of repo_slugs that are ready.
    """
    # Group rows by unique (repo_url, commit_hash) to avoid duplicate work
    repo_groups = {}
    for row_num in selected_rows:
        target = target_map.get(row_num)
        if target is None:
            continue
        key = (target["repo_url"], target["commit"])
        if key not in repo_groups:
            repo_groups[key] = target
        # All rows sharing this key use the same clone_dir

    ready_repos = set()  # clone_dirs that are verified ready

    for (repo_url, commit_hash), ref_target in repo_groups.items():
        clone_dir = ref_target["clone_dir"]
        slug = ref_target["repo_slug"]

        # 1. Check if repo exists
        if not os.path.isdir(os.path.join(clone_dir, ".git")):
            print(f"\n  {slug}: cloning from {repo_url} ...")
            os.makedirs(REPOS_DIR, exist_ok=True)
            rc, out, err = _run_shell(f"git clone '{repo_url}' '{clone_dir}'", timeout=600)
            if rc != 0:
                print(f"    CLONE FAILED: {err[:200]}")
                continue
            print(f"    Cloned OK")

        # 2. Check commit
        rc, current_head, _ = _run_shell(f"git rev-parse HEAD", cwd=clone_dir)
        if rc != 0:
            print(f"\n  {slug}: cannot read HEAD, skipping")
            continue

        if not current_head.lower().startswith(commit_hash.lower()):
            print(f"\n  {slug}: checking out {commit_hash[:8]} (was {current_head[:8]}) ...")
            rc, _, err = _run_shell(
                f"git checkout --no-recurse-submodules '{commit_hash}'",
                cwd=clone_dir, timeout=1200,
            )
            if rc != 0:
                # Try with clean + force
                _run_shell("git clean -fd", cwd=clone_dir)
                rc, _, err = _run_shell(
                    f"git checkout -f --no-recurse-submodules '{commit_hash}'",
                    cwd=clone_dir, timeout=1200,
                )
                if rc != 0:
                    print(f"    CHECKOUT FAILED: {err[:200]}")
                    continue
            # Verify
            rc2, head2, _ = _run_shell("git rev-parse HEAD", cwd=clone_dir)
            if not head2.lower().startswith(commit_hash.lower()):
                print(f"    HEAD MISMATCH: expected {commit_hash[:8]}, got {head2[:8]}")
                continue
            print(f"    Checkout OK")

        # 3. Detect Java version and mvn command
        java_ver, assumed = _detect_java_version(clone_dir)
        mvn_cmd = _detect_mvn_cmd(clone_dir)

        # Update ALL targets sharing this clone_dir
        for row_num in selected_rows:
            t = target_map.get(row_num)
            if t and t["clone_dir"] == clone_dir:
                t["required_java_version"] = java_ver
                t["mvn_cmd"] = mvn_cmd
                t["test_src_found"] = os.path.isfile(os.path.join(BASE_DIR, t["test_src_path"]))

        ready_repos.add(clone_dir)

    return ready_repos


def preflight_precompile(target_map, selected_rows):
    """Ensure test-classes exist for each maven module."""
    compiled_repos = set()
    for row_num in selected_rows:
        target = target_map.get(row_num)
        if target is None:
            continue
        clone_dir = target["clone_dir"]
        if clone_dir not in compiled_repos and not os.path.isdir(os.path.join(clone_dir, ".git")):
            compiled_repos.add(os.path.join(BASE_DIR, target["maven_module_path"]))
            continue  # repo not available, skip
        maven_mod_abs = os.path.join(BASE_DIR, target["maven_module_path"])
        if maven_mod_abs in compiled_repos:
            continue
        tc_dir = os.path.join(maven_mod_abs, "target", "test-classes")
        has_classes = os.path.isdir(tc_dir) and bool(
            _glob.glob(os.path.join(tc_dir, "**", "*.class"), recursive=True)
        )
        if not has_classes:
            print(f"Pre-compiling {target['repo_slug']} ({target['package']}) — test-classes missing...")
            env = make_env(target.get("required_java_version", 8))
            mvn = target.get("mvn_cmd", "mvn")
            pre_log = os.path.join(VAL_S5_DIR, f"_precompile_{target['repo_slug']}.log")
            os.makedirs(os.path.dirname(pre_log), exist_ok=True)
            cmd = f"{mvn} test-compile -B {SKIP_FLAGS}"
            rc, _, elapsed = run_cmd(
                cmd,
                os.path.join(BASE_DIR, target["repo_local_path"]),
                1800, pre_log, env,
            )
            if rc == 0:
                print(f"  Pre-compile OK ({elapsed}s)")
            else:
                print(f"  Pre-compile FAILED ({elapsed}s) — see {pre_log}")
        compiled_repos.add(maven_mod_abs)


# ── Phase 2: Patch discovery ─────────────────────────────────────────────────

def find_best_patch(row_key, target, dirs):
    """Find the most recent usable patch for a row.

    Returns (patched_content, source_label, fix_code, imports)
    or (None, skip_reason, None, None) if no patch available.
    """
    s5_dir = dirs["s5_dir"]
    s4_dir = dirs["s4_dir"]
    s2_dir = dirs["s2_dir"]

    # 1. Check section5 attempts (highest attempt number first)
    s5_row = os.path.join(s5_dir, row_key)
    if os.path.isdir(s5_row):
        attempts = sorted(
            [d for d in os.listdir(s5_row) if d.startswith("attempt") and
             os.path.isdir(os.path.join(s5_row, d))],
            key=lambda x: int(x.replace("attempt", "")),
            reverse=True,
        )
        for attempt_name in attempts:
            attempt_dir = os.path.join(s5_row, attempt_name)
            patched = os.path.join(attempt_dir, "patched_test.java")
            if os.path.isfile(patched):
                content = read_file(patched).strip()
                if content:
                    attempt_json = load_json(os.path.join(attempt_dir, "attempt.json"))
                    fix_code = attempt_json.get("fix_code", "")
                    imports = attempt_json.get("imports", "")
                    return content, f"s5_{attempt_name}", fix_code, imports

    # 2. Check section4 compilation
    s4_row = os.path.join(s4_dir, row_key)
    if os.path.isdir(s4_row):
        stitch_status_path = os.path.join(s4_row, "stitch_compile_status.txt")
        compile_status_path = os.path.join(s4_row, "compile_status.txt")

        stitch_status = read_file(stitch_status_path).strip() if os.path.isfile(stitch_status_path) else ""
        compile_status = read_file(compile_status_path).strip() if os.path.isfile(compile_status_path) else ""

        if stitch_status == "PASS":
            stitched = os.path.join(s4_row, "stitched_test.java")
            if os.path.isfile(stitched):
                content = read_file(stitched)
                # Load fix_code/imports from s2 parsed
                fc = read_file(os.path.join(s2_dir, row_key, "fix_code.java")).strip()
                im = read_file(os.path.join(s2_dir, row_key, "imports.txt")).strip()
                return content, "s4_stitched", fc, im

        if compile_status == "PASS":
            patched = os.path.join(s4_row, "patched_test.java")
            if os.path.isfile(patched):
                content = read_file(patched)
                fc = read_file(os.path.join(s2_dir, row_key, "fix_code.java")).strip()
                im = read_file(os.path.join(s2_dir, row_key, "imports.txt")).strip()
                return content, "s4_initial", fc, im

    # 3. Section 2 rebuild (last resort)
    s2_row = os.path.join(s2_dir, row_key)
    parse_status = read_file(os.path.join(s2_row, "parse_status.txt")).strip()
    if parse_status == "OK":
        fc = read_file(os.path.join(s2_row, "fix_code.java")).strip()
        im = read_file(os.path.join(s2_row, "imports.txt")).strip()
        if fc:
            test_src_abs = os.path.join(BASE_DIR, target["test_src_path"])
            if os.path.isfile(test_src_abs):
                original_text = read_file(test_src_abs)
                fc, im = extract_imports_from_fix_code(fc, im)
                base = build_patched_content(
                    original_text, fc,
                    target["victim_method"], target["polluter_method"],
                )
                if base is not None:
                    content = insert_imports(base, im)
                    return content, "s2_rebuilt", fc, im

    return None, "no_patch_found", None, None


# ── Phase 3: Validation execution ────────────────────────────────────────────

def validate_row(row_key, target, patched_content, source_label, fix_code,
                 imports, val_s5_dir):
    """Compile and test one patch (single attempt, no retries).

    Returns (final_status, attempt_metrics_dict).
    """
    out = os.path.join(val_s5_dir, row_key)
    os.makedirs(out, exist_ok=True)
    attempt_dir = os.path.join(out, "attempt1")
    os.makedirs(attempt_dir, exist_ok=True)

    test_src_rel  = target["test_src_path"]
    test_src_abs  = os.path.join(BASE_DIR, test_src_rel)
    maven_mod_abs = os.path.join(BASE_DIR, target["maven_module_path"])

    env = make_env(target.get("required_java_version", 8))

    am = {
        "attempt_num":                  1,
        "source":                       source_label,
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

    final_status = None

    try:
        # Save patched file
        write_file(os.path.join(attempt_dir, "patched_test.java"), patched_content)

        # Compile (matches original section5 do_compile exactly)
        print(f"        Compiling...", flush=True)
        write_file(test_src_abs, patched_content)
        invalidate_class_cache(maven_mod_abs, test_src_rel)
        compile_log = os.path.join(attempt_dir, "compile.log")
        cmd = f"mvn test-compile -B -o {SKIP_FLAGS}"
        rc, output, elapsed = run_cmd(cmd, maven_mod_abs, COMPILE_TIMEOUT, compile_log, env)
        c_status = compile_status_from_output(rc, output, test_src_rel)
        am["compile_elapsed_seconds"] = elapsed

        if c_status != "PASS":
            am["compiled"] = False
            am["attempt_result"] = "COMPILE_ERROR"
            final_status = "COMPILE_ERROR"
            print(f"        Compile FAIL", flush=True)
        else:
            am["compiled"] = True

            # Run OD test
            print(f"        Running OD test...", flush=True)
            test_log = os.path.join(attempt_dir, "test_run.log")
            test_stat, test_output, test_elapsed = run_od_test(target, test_log, env)
            am["test_result"] = test_stat
            am["test_elapsed_seconds"] = test_elapsed
            print(f"        Test: {test_stat}", flush=True)

            if test_stat == "PASSED":
                # Victim alone
                print(f"        Checking victim alone...", flush=True)
                va_log = os.path.join(attempt_dir, "victim_alone.log")
                va_stat, _, va_elapsed = run_victim_alone(target, va_log, env)
                am["victim_alone_result"] = va_stat
                am["victim_alone_elapsed_seconds"] = va_elapsed

                # Polluter alone
                print(f"        Checking polluter alone...", flush=True)
                pa_log = os.path.join(attempt_dir, "polluter_alone.log")
                pa_stat, _, pa_elapsed = run_polluter_alone(target, pa_log, env)
                am["polluter_alone_result"] = pa_stat
                am["polluter_alone_elapsed_seconds"] = pa_elapsed

                if va_stat != "PASSED":
                    print(f"        victim alone FAILED — NOT_FIXED", flush=True)
                    am["attempt_result"] = "NOT_FIXED"
                    final_status = "NOT_FIXED"
                else:
                    am["attempt_result"] = "FIXED"
                    final_status = "FIXED"
            else:
                am["attempt_result"] = "NOT_FIXED"
                final_status = "NOT_FIXED"

    except Exception as e:
        print(f"        ERROR: {e}", flush=True)
        am["attempt_result"] = "COMPILE_ERROR"
        final_status = "COMPILE_ERROR"

    finally:
        restore_source(target)

    # Write attempt.json
    attempt_data = {
        "source":               source_label,
        "compile_status":       "PASS" if am["compiled"] else "FAIL",
        "stitch_status":        None,
        "test_status":          am.get("test_result"),
        "attempt_result":       am.get("attempt_result") or "COMPILE_ERROR",
        "fix_code":             fix_code or "",
        "imports":              imports or "",
        "victim_alone_status":  am.get("victim_alone_result"),
        "polluter_alone_status": am.get("polluter_alone_result"),
    }
    with open(os.path.join(attempt_dir, "attempt.json"), "w", encoding="utf-8") as f:
        json.dump(attempt_data, f, indent=2)

    # Write attempt_result.txt (for section6 find_winning_patched_file compat)
    write_file(
        os.path.join(attempt_dir, "attempt_result.txt"),
        (am.get("attempt_result") or "COMPILE_ERROR") + "\n",
    )

    # Write row_result.json
    row_result = {
        "final_status": final_status or "COMPILE_ERROR",
        "attempt_metrics": [am],
    }
    with open(os.path.join(out, "row_result.json"), "w", encoding="utf-8") as f:
        json.dump(row_result, f, indent=2)

    return final_status or "COMPILE_ERROR", am


# ── Phase 4: Results generation ──────────────────────────────────────────────

def write_validation_category(row_key, condition, category, passed, needs_review):
    out = os.path.join(VAL_S6_DIR, f"{row_key}_{condition}")
    os.makedirs(out, exist_ok=True)
    for name, val in [
        ("category.txt",     category),
        ("passed.txt",       passed),
        ("needs_review.txt", str(needs_review).lower()),
    ]:
        write_file(os.path.join(out, name), val + "\n")


def generate_categorization(validated_rows, target_map):
    """Run section6-style categorization on validation results."""
    print("\n" + "=" * 65)
    print("CATEGORIZATION")
    print("=" * 65)

    for row_key, condition, final_status in validated_rows:
        row_num = int(row_key.replace("row", ""))
        target = target_map.get(row_num)
        if target is None:
            continue

        cond_dirs = CONDITIONS[condition]
        cat_dirs = {
            "s2_dir": cond_dirs["s2_dir"],       # original parsed dir (read-only)
            "s5_dir": cond_dirs["val_s5_dir"],    # validation test runs
        }

        cat, passed, needs_rev = categorize(row_key, target, cat_dirs)
        write_validation_category(row_key, condition, cat, passed, needs_rev)
        flag = " *** NEEDS_REVIEW" if needs_rev else ""
        print(f"  {row_key} [{condition:10}] -> {cat}{flag}", flush=True)


def _load_original_run_data(row_key, condition):
    """Read the original section5 row_result.json and section1 metrics
    to get attempt count, total tokens, and total elapsed seconds."""
    cond_dirs = CONDITIONS[condition]

    # Original section5 attempt data
    orig_s5 = os.path.join(cond_dirs["s5_dir"], row_key, "row_result.json")
    orig_result = load_json(orig_s5)
    attempt_list = orig_result.get("attempt_metrics", [])
    original_attempts = len(attempt_list)

    # Original section1 token/time data
    s1_metrics = load_json(os.path.join(cond_dirs["s1_dir"], "metrics.json"))
    s1 = s1_metrics.get(row_key, {})
    s1_tokens = s1.get("total_tokens", 0) or 0
    s1_api_sec = s1.get("elapsed_seconds", 0) or 0

    # Sum all tokens and time from section5 attempts (retries + stitches)
    s5_tokens = sum((a.get("gpt4_total_tokens") or 0) + (a.get("stitch_total_tokens") or 0)
                    for a in attempt_list)
    s5_compile = sum((a.get("compile_elapsed_seconds") or 0) +
                     (a.get("stitch_compile_elapsed_seconds") or 0)
                     for a in attempt_list)
    s5_api = sum((a.get("gpt4_api_elapsed_seconds") or 0) +
                 (a.get("stitch_api_elapsed_seconds") or 0)
                 for a in attempt_list)
    s5_test = sum((a.get("test_elapsed_seconds") or 0) +
                  (a.get("victim_alone_elapsed_seconds") or 0) +
                  (a.get("polluter_alone_elapsed_seconds") or 0)
                  for a in attempt_list)

    original_total_tokens = s1_tokens + s5_tokens
    original_total_sec = round(s1_api_sec + s5_api + s5_compile + s5_test, 2)

    return original_attempts, original_total_tokens, original_total_sec


VALIDATION_CSV_FIELDS = [
    "test_id", "row_num", "condition", "victim", "polluter", "repo_url", "commit",
    "final_status", "category", "passed",
    "validation_patch_source",
    "validation_compile_sec", "validation_test_sec",
    "validation_victim_alone_sec", "validation_polluter_alone_sec",
    "validation_total_sec",
    "original_attempts", "original_total_tokens", "original_total_sec",
]


def _build_csv_row(row_key, condition, target, source_label, am, category, passed):
    """Build one row dict for the validation CSV."""
    row_num = int(row_key.replace("row", ""))

    # Validation times
    v_compile = (am or {}).get("compile_elapsed_seconds")
    v_test = (am or {}).get("test_elapsed_seconds")
    v_va = (am or {}).get("victim_alone_elapsed_seconds")
    v_pa = (am or {}).get("polluter_alone_elapsed_seconds")
    v_total = round(sum(v for v in [v_compile, v_test, v_va, v_pa] if v is not None), 2) or None

    # Original run data
    orig_attempts, orig_tokens, orig_sec = _load_original_run_data(row_key, condition)

    return {
        "test_id":                   target["test_id"],
        "row_num":                   row_num,
        "condition":                 condition,
        "victim":                    target.get("victim", ""),
        "polluter":                  target.get("polluter", ""),
        "repo_url":                  target.get("repo_url", ""),
        "commit":                    target.get("commit", ""),
        "final_status":              (am or {}).get("attempt_result") or "SKIPPED",
        "category":                  category,
        "passed":                    passed,
        "validation_patch_source":   source_label,
        "validation_compile_sec":    v_compile,
        "validation_test_sec":       v_test,
        "validation_victim_alone_sec": v_va,
        "validation_polluter_alone_sec": v_pa,
        "validation_total_sec":      v_total,
        "original_attempts":         orig_attempts,
        "original_total_tokens":     orig_tokens,
        "original_total_sec":        orig_sec,
    }


def generate_csv(results, skip_log, target_map):
    """Write validation-specific CSV with lean columns."""
    print("\n" + "=" * 65)
    print("CSV ASSEMBLY")
    print("=" * 65)

    # Clear previous results to avoid stale files
    if os.path.isdir(VAL_S7_DIR):
        import shutil as _sh
        _sh.rmtree(VAL_S7_DIR)
    os.makedirs(VAL_S7_DIR)

    all_rows = []
    for row_key, condition, final_status, source_label, am in results:
        row_num = int(row_key.replace("row", ""))
        target = target_map.get(row_num)
        if target is None:
            continue

        # Read category from validation_categories
        cat_dir = os.path.join(VAL_S6_DIR, f"{row_key}_{condition}")
        category = read_file(os.path.join(cat_dir, "category.txt")).strip() or "MISSING"
        passed = read_file(os.path.join(cat_dir, "passed.txt")).strip() or "N"

        row_data = _build_csv_row(row_key, condition, target, source_label, am,
                                  category, passed)
        all_rows.append(row_data)

    if all_rows:
        results_path = os.path.join(VAL_S7_DIR, "results.csv")
        with open(results_path, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=VALIDATION_CSV_FIELDS)
            w.writeheader()
            for row in sorted(all_rows, key=lambda r: (r["row_num"], r["condition"])):
                w.writerow(row)
        print(f"Wrote {results_path}  ({len(all_rows)} rows)")

    # Write skip log
    if skip_log:
        skip_path = os.path.join(VAL_S7_DIR, "skip_log.csv")
        with open(skip_path, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["row_key", "condition", "reason"])
            for entry in sorted(skip_log, key=lambda x: (x[0], x[1])):
                w.writerow(entry)
        print(f"Wrote {skip_path}  ({len(skip_log)} skipped)")


# ── Main ──────────────────────────────────────────────────────────────────────

def parse_args():
    parser = argparse.ArgumentParser(
        description="Validate existing patches without an OpenAI API key.",
    )
    parser.add_argument(
        "--rows", nargs="*", metavar="N",
        help="Row numbers to validate (e.g. 1 2 3-5). Default: all rows.",
    )
    parser.add_argument(
        "--skip-preflight", action="store_true",
        help="Skip repo availability and pre-build checks.",
    )
    return parser.parse_args()


def main():
    args = parse_args()

    print("=" * 65)
    print("PATCH VALIDATION (no API key required)")
    print("=" * 65)
    print()

    # ── Phase 1: Pre-flight ───────────────────────────────────────────────
    preflight_api_check()

    # Build targets from CSV (not manifest — works for all 50 rows)
    target_map = load_targets_from_csv()

    # Determine selected rows
    if args.rows:
        selected_rows = parse_rows_arg(args.rows)
    else:
        selected_rows = sorted(target_map.keys())

    # Filter to rows that exist in CSV
    selected_rows = [r for r in selected_rows if r in target_map]

    print(f"Rows: {selected_rows}")
    print(f"Conditions: with_repro, no_repro")
    print()

    # ── Patch availability check ──────────────────────────────────────────
    # Check which rows have patches BEFORE cloning/building repos.
    # Patch discovery only reads from section outputs (no repo needed).
    print("Checking patch availability...")
    rows_with_patches = set()
    rows_without_patches = set()
    for row_num in selected_rows:
        row_key = f"row{row_num:02d}"
        target = target_map.get(row_num)
        if target is None:
            continue
        has_patch = False
        for condition, cond_dirs in CONDITIONS.items():
            # Quick check: does any patch exist in s5, s4, or s2?
            s5_row = os.path.join(cond_dirs["s5_dir"], row_key)
            s4_row = os.path.join(cond_dirs["s4_dir"], row_key)
            s2_row = os.path.join(cond_dirs["s2_dir"], row_key)
            if (os.path.isdir(s5_row) or os.path.isdir(s4_row) or
                    os.path.isfile(os.path.join(s2_row, "fix_code.java"))):
                has_patch = True
                break
        if has_patch:
            rows_with_patches.add(row_num)
        else:
            rows_without_patches.add(row_num)

    if rows_without_patches:
        print(f"  No patches found for rows: {sorted(rows_without_patches)}")
        print(f"  These rows will be skipped.")
    print(f"  Patches available for {len(rows_with_patches)}/{len(selected_rows)} rows")

    if not rows_with_patches:
        print("\nERROR: No patches found for any selected row. Nothing to validate.")
        sys.exit(1)

    # Only process rows that have patches
    selected_rows = [r for r in selected_rows if r in rows_with_patches]
    print()

    skip_log = []  # list of (row_key, condition, reason)

    # Log rows without patches
    for row_num in sorted(rows_without_patches):
        row_key = f"row{row_num:02d}"
        for condition in CONDITIONS:
            skip_log.append((row_key, condition, "no_patch_found"))

    if not args.skip_preflight:
        ready_repos = preflight_repo_check(target_map, selected_rows)
        preflight_precompile(target_map, selected_rows)
    else:
        print("Pre-flight checks skipped (--skip-preflight).\n")
        ready_repos = None  # unknown — will check per-row

    # Restore all source files before starting
    print("Restoring all source files to git state...")
    for row_num in selected_rows:
        target = target_map.get(row_num)
        if target and os.path.isdir(os.path.join(target["clone_dir"], ".git")):
            restore_source(target)

    # Create output directories
    for d in (VAL_S5_DIR, VAL_S5_NO_REPRO_DIR, VAL_S6_DIR, VAL_S7_DIR):
        os.makedirs(d, exist_ok=True)

    # ── Phase 2 + 3: Discover patches and validate ────────────────────────
    print("\n" + "=" * 65)
    print("VALIDATION EXECUTION")
    print("=" * 65)

    validated_rows = []  # list of (row_key, condition, final_status)
    results = []         # list of (row_key, condition, final_status, source_label, am)

    for row_num in selected_rows:
        row_key = f"row{row_num:02d}"
        target = target_map.get(row_num)

        if target is None:
            print(f"\n  {row_key}: SKIP — no target metadata")
            for condition in CONDITIONS:
                skip_log.append((row_key, condition, "no_target_metadata"))
            continue

        # Check repo is present and at correct commit
        clone_dir = target["clone_dir"]
        if not os.path.isdir(os.path.join(clone_dir, ".git")):
            print(f"\n  {row_key}: SKIP — repo not cloned: {os.path.relpath(clone_dir, BASE_DIR)}")
            for condition in CONDITIONS:
                skip_log.append((row_key, condition, "repo_missing"))
            continue

        test_src_abs = os.path.join(BASE_DIR, target["test_src_path"])
        if not os.path.isfile(test_src_abs):
            print(f"\n  {row_key}: SKIP — test source missing: {target['test_src_path']}")
            for condition in CONDITIONS:
                skip_log.append((row_key, condition, "test_src_missing"))
            continue

        for condition, cond_dirs in CONDITIONS.items():
            print(f"\n  {row_key} [{condition}] ({target['test_id']})", flush=True)

            # Discover patch
            patched_content, source_label, fix_code, imports = find_best_patch(
                row_key, target, cond_dirs,
            )

            if patched_content is None:
                print(f"        SKIP — {source_label}")
                skip_log.append((row_key, condition, source_label))

                # Write a SKIPPED row_result so section6/7 can process it
                val_s5 = cond_dirs["val_s5_dir"]
                skip_out = os.path.join(val_s5, row_key)
                os.makedirs(skip_out, exist_ok=True)
                with open(os.path.join(skip_out, "row_result.json"), "w") as f:
                    json.dump({"final_status": "SKIPPED", "attempt_metrics": []}, f, indent=2)

                validated_rows.append((row_key, condition, "SKIPPED"))
                results.append((row_key, condition, "SKIPPED", source_label, None))
                continue

            print(f"        Patch source: {source_label}")

            # Validate
            final_status, am = validate_row(
                row_key, target, patched_content, source_label,
                fix_code, imports, cond_dirs["val_s5_dir"],
            )

            validated_rows.append((row_key, condition, final_status))
            results.append((row_key, condition, final_status, source_label, am))
            print(f"        -> {final_status}", flush=True)

    # ── Phase 4: Categorization + CSV ─────────────────────────────────────
    generate_categorization(validated_rows, target_map)
    generate_csv(results, skip_log, target_map)

    # Clean up intermediate directories (categories and test run artifacts)
    import shutil
    for d in (VAL_S5_DIR, VAL_S5_NO_REPRO_DIR, VAL_S6_DIR):
        if os.path.isdir(d):
            shutil.rmtree(d)

    # ── Summary table ─────────────────────────────────────────────────────
    print("\n" + "=" * 90)
    print(f"{'row':<8} {'condition':<12} {'status':<16} {'patch_source':<20} "
          f"{'compile':>8} {'test':>8}")
    print("-" * 90)

    counts = {"FIXED": 0, "NOT_FIXED": 0, "COMPILE_ERROR": 0, "SKIPPED": 0}
    for row_key, condition, final_status, source_label, am in results:
        c_sec = f"{am['compile_elapsed_seconds']:.1f}s" if am and am.get("compile_elapsed_seconds") else "-"
        t_sec = f"{am['test_elapsed_seconds']:.1f}s" if am and am.get("test_elapsed_seconds") else "-"
        print(f"{row_key:<8} {condition:<12} {final_status:<16} {source_label:<20} "
              f"{c_sec:>8} {t_sec:>8}")
        counts[final_status] = counts.get(final_status, 0) + 1

    print("=" * 90)
    print(f"\nFIXED         : {counts.get('FIXED', 0)}")
    print(f"NOT_FIXED     : {counts.get('NOT_FIXED', 0)}")
    print(f"COMPILE_ERROR : {counts.get('COMPILE_ERROR', 0)}")
    print(f"SKIPPED       : {counts.get('SKIPPED', 0)}")

    print(f"\nOutputs:")
    print(f"  Test runs     : {VAL_S5_DIR}/")
    if any(c == "no_repro" for _, c, _ in validated_rows):
        print(f"                  {VAL_S5_NO_REPRO_DIR}/")
    print(f"  Categories    : {VAL_S6_DIR}/")
    print(f"  Results CSV   : {VAL_S7_DIR}/")

    print("\nVALIDATION COMPLETE")


if __name__ == "__main__":
    main()
