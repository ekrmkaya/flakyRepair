#!/usr/bin/env python3
"""
Step 2: Pre-Build — Compile all targeted repos before any reproduction.

Runs `mvn test-compile -pl '{package}' -am -B {SKIP_FLAGS}` once per unique
(clone_dir, package) pair. This warms Maven's local repository cache so that
subsequent surefire:test invocations in step3 can skip the compile phase.

Includes the same auto-retry logic as the original section3_build_validation.py:
  Retry A — Java version mismatch (-Dmaven.compiler.source/target override)
  Retry B — Artifact resolution failure (dependency:resolve then retry)

Step 2 status values:
  BUILD_OK      — test-compile succeeded (possibly after a retry)
  BUILD_FAILED  — test-compile failed after all retries
  JAVA_NOT_FOUND— no suitable JDK available for this repo
  (inherits CLONE_FAILED / CHECKOUT_FAILED / NO_POM_FOUND from step1)
"""

import glob as globmod
import json
import os
import re
import subprocess
import sys
import time

from config import SKIP_FLAGS, java_home_for, make_env


# ── Helpers ────────────────────────────────────────────────────────────────────

def _run(cmd, cwd=None, env=None, timeout=1200):
    r = subprocess.run(cmd, shell=True, capture_output=True, text=True,
                       cwd=cwd, env=env, timeout=timeout)
    return r.returncode, r.stdout, r.stderr


def _write_log(path, content):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


def _write_manifest(pairs, manifest_path):
    os.makedirs(os.path.dirname(manifest_path), exist_ok=True)
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump({"pairs": pairs}, f, indent=2)


# ── Maven wrapper detection ────────────────────────────────────────────────────

def _detect_mvn(clone_dir, env):
    mvnw = os.path.join(clone_dir, "mvnw")
    if os.path.isfile(mvnw):
        os.chmod(mvnw, 0o755)
        mvn_cmd = "./mvnw"
        # Parse version from wrapper properties
        wp = os.path.join(clone_dir, ".mvn", "wrapper", "maven-wrapper.properties")
        mv = None
        if os.path.isfile(wp):
            for line in open(wp):
                m = re.search(r'apache-maven-([\d.]+)-bin', line)
                if m:
                    mv = m.group(1)
                    break
        if not mv:
            rc, o, _ = _run(f"{mvnw} --version 2>/dev/null", cwd=clone_dir, env=env)
            m = re.search(r'Apache Maven ([\d.]+)', o)
            mv = m.group(1) if m else "unknown"
    else:
        mvn_cmd = "mvn"
        rc, o, _ = _run("mvn --version", env=env)
        m = re.search(r'Apache Maven ([\d.]+)', o)
        mv = m.group(1) if m else "unknown"
    return mvn_cmd, mv


def _is_already_compiled(clone_dir, package):
    """Return True if test-classes .class files already exist for this module.

    Files whose basename contains a space (e.g. 'Foo 2.class') are macOS
    filesystem deduplication artefacts.  If ANY such files exist (whether or
    not the original also exists), treat the module as not yet compiled so
    step2 runs 'clean test-compile' to clear all artefacts including the
    space-named copies.  Surefire's directory scanner chokes on them even when
    the original .class is present alongside the duplicates.
    """
    pkg_dir = os.path.join(clone_dir, package) if package not in (".", "") else clone_dir
    tc_dir  = os.path.join(pkg_dir, "target", "test-classes")
    if not os.path.isdir(tc_dir):
        return False
    class_files = globmod.glob(os.path.join(tc_dir, "**", "*.class"), recursive=True)
    corrupt = [f for f in class_files if " " in os.path.basename(f)]
    if corrupt:
        return False   # Force clean recompile to remove space-named duplicates
    valid = [f for f in class_files if " " not in os.path.basename(f)]
    return bool(valid)


# ── Build one (clone_dir, package) target ─────────────────────────────────────

def _build_target(rep, logs_dir):
    """
    Run test-compile for one (clone_dir, package) representative pair.
    Returns a result dict with status, mvn_cmd, maven_version, java_home_used,
    java_version_used, java_fallback_note, compile_sec, compile_log.
    """
    clone_dir = rep["clone_dir"]
    package   = rep["package"]
    slug      = rep["repo_slug"]
    req_java  = rep.get("required_java_version", 8)

    result = {
        "mvn_cmd":           None,
        "maven_version":     None,
        "java_home_used":    None,
        "java_version_used": None,
        "java_fallback_note":None,
        "step2_status":      None,
        "compile_sec":       0.0,
        "compile_log":       None,
    }

    # Java
    java_home, actual_java, java_note = java_home_for(req_java)
    if java_home is None:
        print(f"  JAVA_NOT_FOUND: {java_note}")
        result["step2_status"] = "JAVA_NOT_FOUND"
        return result

    result["java_home_used"]    = java_home
    result["java_version_used"] = actual_java
    result["java_fallback_note"]= java_note
    env = make_env(java_home)

    if java_note:
        print(f"  NOTE: {java_note}")

    # Maven
    mvn_cmd, mv = _detect_mvn(clone_dir, env)
    result["mvn_cmd"]       = mvn_cmd
    result["maven_version"] = mv
    print(f"  Maven: {mvn_cmd} {mv}  Java: {actual_java}")

    # Skip if already compiled
    if _is_already_compiled(clone_dir, package):
        print(f"  SKIP: test-classes already present")
        result["step2_status"] = "BUILD_OK"
        result["compile_sec"]  = 0.0
        return result

    # Build command
    pl_flags  = f"-pl '{package}' -am" if package not in (".", "") else ""
    build_cmd = f"{mvn_cmd} {pl_flags} clean test-compile -B -q {SKIP_FLAGS}"
    log_path  = os.path.join(logs_dir, f"{slug}_build.log")
    result["compile_log"] = log_path

    print(f"  Building … (may take a while)", flush=True)
    t0 = time.time()

    try:
        rc, out, err = _run(build_cmd, cwd=clone_dir, env=env, timeout=1200)
    except subprocess.TimeoutExpired:
        rc, out, err = 1, "", "TIMEOUT after 1200s"

    log_content = (f"CMD: {build_cmd}\nRC: {rc}\n"
                   f"ELAPSED: {time.time()-t0:.0f}s\n\nSTDOUT:\n{out}\nSTDERR:\n{err}")

    if rc != 0:
        print(f"  Build failed — checking retries …")

        # Retry A: Java version mismatch
        if "source release" in (out+err) or "target release" in (out+err):
            print(f"  Retry A: Java version override …")
            ov = (f"-Djava.version={actual_java} "
                  f"-Dmaven.compiler.source={actual_java} "
                  f"-Dmaven.compiler.target={actual_java}")
            retry_cmd = f"{mvn_cmd} {pl_flags} {ov} clean test-compile -B -q {SKIP_FLAGS}"
            try:
                rc, out, err = _run(retry_cmd, cwd=clone_dir, env=env, timeout=1200)
            except subprocess.TimeoutExpired:
                rc, out, err = 1, "", "TIMEOUT"
            log_content += f"\n\n--- RETRY A ---\nCMD: {retry_cmd}\nRC: {rc}\n{out}\n{err}"

        # Retry B: dependency resolution failure
        if rc != 0 and ("Could not resolve" in (out+err) or "artifact" in (out+err).lower()):
            print(f"  Retry B: dependency:resolve then rebuild …")
            _run(f"{mvn_cmd} {pl_flags} dependency:resolve -B -q",
                 cwd=clone_dir, env=env, timeout=600)
            try:
                rc, out, err = _run(build_cmd, cwd=clone_dir, env=env, timeout=1200)
            except subprocess.TimeoutExpired:
                rc, out, err = 1, "", "TIMEOUT"
            log_content += f"\n\n--- RETRY B ---\nRC: {rc}\n{out}\n{err}"

    result["compile_sec"] = time.time() - t0
    _write_log(log_path, log_content)

    if rc == 0:
        result["step2_status"] = "BUILD_OK"
        print(f"  BUILD OK  ({result['compile_sec']:.0f}s)")
    else:
        result["step2_status"] = "BUILD_FAILED"
        errors = [l for l in (out+"\n"+err).splitlines()
                  if "ERROR" in l or "error" in l.lower()][:5]
        result["build_error_summary"] = "\n".join(errors)
        print(f"  BUILD FAILED ({result['compile_sec']:.0f}s)")
        for e in errors[:3]:
            print(f"    {e[:120]}")

    return result


# ── Main step function ─────────────────────────────────────────────────────────

def run_step2(pairs, manifest_path, logs_dir):
    """
    Pre-compile all READY repos. Updates pairs in-place with step2_status
    and timing. Writes updated manifest.
    """
    os.makedirs(logs_dir, exist_ok=True)

    print("\n" + "=" * 70)
    print("STEP 2: Pre-Build — Compile All Repos")
    print("=" * 70)

    ready = [p for p in pairs if p.get("step1_status") == "READY"]
    print(f"{len(ready)} READY pairs to build\n")

    # Deduplicate by (clone_dir, package)
    build_key  = lambda p: (p["clone_dir"], p["package"])
    unique_builds = {}
    for p in ready:
        k = build_key(p)
        if k not in unique_builds:
            unique_builds[k] = p

    print(f"Unique (clone_dir, package) targets: {len(unique_builds)}\n")

    build_results = {}
    for idx, (bkey, rep) in enumerate(unique_builds.items(), 1):
        print(f"BUILD {idx}/{len(unique_builds)}: {rep['repo_slug']}  pkg={rep['package']}")
        result = _build_target(rep, logs_dir)
        build_results[bkey] = result

    # Propagate results to all rows
    for p in pairs:
        if p.get("step1_status") != "READY":
            continue
        k = build_key(p)
        if k not in build_results:
            continue
        r = build_results[k]
        p.update({
            "mvn_cmd":           r["mvn_cmd"],
            "maven_version":     r["maven_version"],
            "java_home_used":    r["java_home_used"],
            "java_version_used": r["java_version_used"],
            "java_fallback_note":r["java_fallback_note"],
            "step2_status":      r["step2_status"],
            "compile_sec":       r["compile_sec"],
            "compile_log":       r.get("compile_log"),
        })
        if "build_error_summary" in r:
            p["build_error_summary"] = r["build_error_summary"]

    _write_manifest(pairs, manifest_path)

    counts = {}
    for p in pairs:
        s = p.get("step2_status") or p.get("step1_status", "?")
        counts[s] = counts.get(s, 0) + 1
    print(f"\nSTEP 2 SUMMARY: {counts}")
    return pairs


# ── Standalone runner ──────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse

    HERE = os.path.dirname(os.path.abspath(__file__))
    _out = os.path.join(HERE, "output")

    ap = argparse.ArgumentParser()
    ap.add_argument("--output-dir", default=_out)
    args = ap.parse_args()

    manifest_path = os.path.join(args.output_dir, "manifest.json")
    logs_dir      = os.path.join(args.output_dir, "logs")

    with open(manifest_path) as f:
        manifest = json.load(f)
    pairs = manifest["pairs"]

    run_step2(pairs, manifest_path, logs_dir)
