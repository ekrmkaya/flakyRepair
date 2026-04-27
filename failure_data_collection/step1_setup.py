#!/usr/bin/env python3
"""
Step 1: CSV Parsing, Repository Clone/Checkout, Java Version Detection,
        Maven Module Location.

Inputs : flaky_tests.csv (path passed in)
Outputs: manifest.json (initial), clone_sec + checkout_sec timing fields

Step 1 status values:
  READY          — all checks passed
  CLONE_FAILED   — git clone/fetch failed
  CHECKOUT_FAILED— git checkout failed or HEAD mismatch
  NO_POM_FOUND   — could not locate a pom.xml for the package
"""

import csv
import glob as globmod
import json
import os
import re
import subprocess
import sys
import time
from collections import defaultdict


# ── Low-level helpers ──────────────────────────────────────────────────────────

def _run(cmd, cwd=None, timeout=600):
    r = subprocess.run(cmd, shell=True, capture_output=True, text=True,
                       cwd=cwd, timeout=timeout)
    return r.returncode, r.stdout.strip(), r.stderr.strip()


def _write_log(path, content):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


# ── CSV parsing ────────────────────────────────────────────────────────────────

REQUIRED_COLS = [
    "repo name", "commit hash", "package", "victim", "polluter",
    "fixed by flakydoctor gpt?",
]


def parse_csv(csv_path, target_rows=None):
    """
    Parse flaky_tests.csv and return a list of pair dicts.

    target_rows: list of 1-based row numbers to include (None = all).
    """
    with open(csv_path, newline="", encoding="utf-8") as f:
        reader = csv.reader(f)
        raw_header = next(reader)
        header_norm = [h.strip().lower() for h in raw_header]
        missing = [c for c in REQUIRED_COLS if c not in header_norm]
        if missing:
            raise ValueError(f"CSV missing required columns: {missing}")
        col = {c: header_norm.index(c) for c in REQUIRED_COLS}

        raw_rows = []
        for csv_row_num, row in enumerate(reader, start=2):
            row_num = csv_row_num - 1  # 1-based data row index
            if target_rows and row_num not in target_rows:
                continue
            repo = row[col["repo name"]].strip() if col["repo name"] < len(row) else ""
            if not repo:
                continue
            victim   = row[col["victim"]].strip()
            polluter = row[col["polluter"]].strip()

            # Derive class and method from FQN.method format
            v_parts  = victim.rsplit(".", 1)
            p_parts  = polluter.rsplit(".", 1)
            if len(v_parts) != 2 or len(p_parts) != 2:
                print(f"  WARNING row {row_num}: malformed victim/polluter FQN, skipping")
                continue

            raw_rows.append({
                "row_key":        f"row{row_num:02d}",
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
                "fixed":          row[col["fixed by flakydoctor gpt?"]].strip().upper()
                                  if col["fixed by flakydoctor gpt?"] < len(row) else "",
            })

    return raw_rows


# ── Clone directory assignment ─────────────────────────────────────────────────

def assign_clone_dirs(pairs, repos_dir):
    """
    Assign clone_dir and repo_slug to each pair.
    If a repo has multiple commits, append the short commit hash to avoid
    directory collisions.
    """
    repo_commits = defaultdict(set)
    for p in pairs:
        repo_commits[p["repo_url"]].add(p["commit_hash"])

    for p in pairs:
        slug_base = p["repo_url"].rstrip("/").split("/")[-1]
        p["repo_slug"] = slug_base
        commits = repo_commits[p["repo_url"]]
        if len(commits) > 1:
            p["clone_dir"] = os.path.join(repos_dir, f"{slug_base}-{p['commit_hash'][:8]}")
        else:
            p["clone_dir"] = os.path.join(repos_dir, slug_base)


# ── Task 1.1: Clone ────────────────────────────────────────────────────────────

def clone_repos(pairs, logs_dir):
    """Clone or fetch each unique (repo_url, commit_hash). Returns status dict."""
    seen = {}
    for p in pairs:
        key = (p["repo_url"], p["commit_hash"])
        if key not in seen:
            seen[key] = p["clone_dir"]

    clone_status = {}
    clone_times  = {}

    for (repo_url, commit_hash), clone_dir in seen.items():
        slug = repo_url.rstrip("/").split("/")[-1]
        key  = (repo_url, commit_hash)
        t0   = time.time()

        if os.path.isdir(clone_dir):
            print(f"  EXISTS  {os.path.basename(clone_dir)} — fetching …")
            # Remove any stale lock before fetch (fetch itself may fail if locked)
            lock = os.path.join(clone_dir, ".git", "index.lock")
            if os.path.isfile(lock):
                os.remove(lock)
            rc, out, err = _run(f"git -C '{clone_dir}' fetch --quiet")
            if rc != 0:
                print(f"    WARNING: fetch failed (continuing): {err[:120]}")
            # Remove any lock left by the fetch before checkout runs
            if os.path.isfile(lock):
                os.remove(lock)
            clone_status[key] = "ok"
        else:
            print(f"  CLONING {repo_url} → {clone_dir}")
            rc, out, err = _run(f"git clone '{repo_url}' '{clone_dir}'", timeout=600)
            log_path = os.path.join(logs_dir, f"{slug}_clone.log")
            _write_log(log_path, f"CMD: git clone {repo_url} {clone_dir}\nRC: {rc}\n"
                                 f"STDOUT:\n{out}\nSTDERR:\n{err}")
            if rc != 0:
                print(f"    FAILED: {err[:200]}")
                clone_status[key] = "failed"
            else:
                print(f"    OK")
                clone_status[key] = "ok"

        clone_times[key] = time.time() - t0

    return clone_status, clone_times


# ── Task 1.2: Checkout ─────────────────────────────────────────────────────────

def checkout_commits(pairs, clone_status, logs_dir):
    """Checkout the target commit in each cloned repo. Returns status dict."""
    seen = {}
    for p in pairs:
        key = (p["repo_url"], p["commit_hash"])
        if key not in seen:
            seen[key] = (p["clone_dir"], p["repo_slug"])

    checkout_status = {}
    checkout_times  = {}

    for (repo_url, commit_hash), (clone_dir, slug) in seen.items():
        key = (repo_url, commit_hash)
        if clone_status.get(key) == "failed":
            checkout_status[key] = "failed"
            checkout_times[key]  = 0.0
            continue

        t0 = time.time()
        print(f"  {slug} @ {commit_hash[:8]} …", end=" ", flush=True)

        log_path = os.path.join(logs_dir, f"{slug}_checkout.log")

        # Remove stale lock file if present
        lock_file = os.path.join(clone_dir, ".git", "index.lock")
        if os.path.isfile(lock_file):
            os.remove(lock_file)

        # If HEAD is already at the target commit, skip the checkout entirely.
        # This avoids re-writing all files for large repos like spring-boot.
        rc_head, current_head, _ = _run(f"git -C '{clone_dir}' rev-parse HEAD")
        if rc_head == 0 and current_head.lower().startswith(commit_hash.lower()):
            print(f"OK (already at {commit_hash[:8]})")
            checkout_status[key] = "ok"
            checkout_times[key]  = time.time() - t0
            continue

        try:
            # First attempt: plain checkout (no-recurse-submodules avoids expensive submodule ops)
            rc, out, err = _run(
                f"git -C '{clone_dir}' checkout --no-recurse-submodules '{commit_hash}'",
                timeout=1200,
            )

            # If untracked files would be overwritten, clean them and retry
            if rc != 0 and "untracked working tree files" in err:
                _run(f"git -C '{clone_dir}' clean -fd")
                rc, out, err = _run(
                    f"git -C '{clone_dir}' checkout --no-recurse-submodules '{commit_hash}'",
                    timeout=1200,
                )

            # If still failing (e.g. dirty tracked files), force checkout
            if rc != 0:
                rc, out, err = _run(
                    f"git -C '{clone_dir}' checkout -f --no-recurse-submodules '{commit_hash}'",
                    timeout=1200,
                )
        except subprocess.TimeoutExpired:
            print("TIMEOUT")
            _write_log(log_path, f"CMD: git checkout {commit_hash}\nRC: TIMEOUT (>1200s)")
            checkout_status[key] = "failed"
            checkout_times[key]  = time.time() - t0
            continue

        if rc != 0:
            print("FAILED")
            _write_log(log_path, f"CMD: git checkout {commit_hash}\nRC:{rc}\n{err}")
            checkout_status[key] = "failed"
            checkout_times[key]  = time.time() - t0
            continue

        # Verify HEAD matches expected commit
        rc2, head, _ = _run(f"git -C '{clone_dir}' rev-parse HEAD")
        if head.lower() != commit_hash.lower():
            msg = f"HEAD mismatch: expected {commit_hash} got {head}"
            print("MISMATCH")
            _write_log(log_path, msg)
            checkout_status[key] = "failed"
            checkout_times[key]  = time.time() - t0
            continue

        # Warn if working tree is dirty (e.g. leftover generated files)
        rc3, wt, _ = _run(f"git -C '{clone_dir}' status --short")
        if wt:
            _write_log(log_path, f"WARNING: dirty working tree:\n{wt}")
            print(f"OK (dirty tree — {log_path})")
        else:
            print("OK")

        checkout_status[key] = "ok"
        checkout_times[key]  = time.time() - t0

    return checkout_status, checkout_times


# ── Task 1.3: Locate pom.xml ───────────────────────────────────────────────────

def find_pom(clone_dir, package):
    """Return path to the closest pom.xml for the given package, or None."""
    if package not in ("", "."):
        candidate = os.path.join(clone_dir, package, "pom.xml")
        if os.path.isfile(candidate):
            return candidate
    # Root-level pom
    root = os.path.join(clone_dir, "pom.xml")
    if os.path.isfile(root):
        return root
    # One level deep
    for p in globmod.glob(os.path.join(clone_dir, "*", "pom.xml")):
        return p
    return None


# ── Task 1.4: Detect Java version ─────────────────────────────────────────────

_java_cache = {}


def detect_java(clone_dir):
    """Return (major_version_int, assumed_bool) for the repo at clone_dir."""
    if clone_dir in _java_cache:
        return _java_cache[clone_dir]

    def norm(v):
        v = str(v).strip().strip('"').strip("'")
        m = re.search(r'(\d+)', v)
        if not m:
            return None
        major = int(m.group(1))
        if major == 1:
            m2 = re.search(r'1\.(\d+)', v)
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

    # 1. .sdkmanrc
    p = os.path.join(clone_dir, ".sdkmanrc")
    if os.path.isfile(p):
        for line in open(p):
            m = re.search(r'java\s*=\s*(\S+)', line)
            if m:
                v = norm(m.group(1))
                if v:
                    _java_cache[clone_dir] = (v, False)
                    return v, False

    # 2. .java-version
    p = os.path.join(clone_dir, ".java-version")
    if os.path.isfile(p):
        v = norm(open(p).read())
        if v:
            _java_cache[clone_dir] = (v, False)
            return v, False

    # 3. .travis.yml
    p = os.path.join(clone_dir, ".travis.yml")
    if os.path.isfile(p):
        for line in open(p, errors="replace"):
            m = re.search(r'jdk\s*:\s*(\S+)', line)
            if m:
                v = norm(m.group(1))
                if v:
                    _java_cache[clone_dir] = (v, False)
                    return v, False

    # 4. .github/workflows/*.yml
    for wf in globmod.glob(os.path.join(clone_dir, ".github", "workflows", "*.yml")):
        for line in open(wf, errors="replace"):
            m = re.search(r'java-version\s*:\s*[\'"]?(\d[\d.]*)', line)
            if m:
                v = norm(m.group(1))
                if v:
                    _java_cache[clone_dir] = (v, False)
                    return v, False

    # 5. pom.xml compiler properties
    root_pom = os.path.join(clone_dir, "pom.xml")
    if os.path.isfile(root_pom):
        content = open(root_pom, errors="replace").read()
        for tag in ("maven.compiler.source", "java.version", "maven.compiler.release"):
            m = re.search(rf'<{tag}>\s*([^<]+)\s*</{tag}>', content)
            if m:
                v = norm(m.group(1))
                if v:
                    _java_cache[clone_dir] = (v, False)
                    return v, False

    _java_cache[clone_dir] = (8, True)
    return 8, True


# ── Main step function ─────────────────────────────────────────────────────────

def run_step1(csv_path, manifest_path, repos_dir, logs_dir, target_rows=None):
    """
    Parse CSV, clone repos, checkout commits, find pom.xml, detect Java version.

    Returns the list of pair dicts with step1_status set.
    Also writes manifest_path.
    """
    os.makedirs(repos_dir,            exist_ok=True)
    os.makedirs(logs_dir,             exist_ok=True)
    os.makedirs(os.path.dirname(manifest_path), exist_ok=True)

    print("=" * 70)
    print("STEP 1: Setup — Clone, Checkout, Java Detection")
    print("=" * 70)

    # ── Parse CSV ──────────────────────────────────────────────────────────────
    pairs = parse_csv(csv_path, target_rows)
    print(f"\nParsed {len(pairs)} pairs from {csv_path}")
    assign_clone_dirs(pairs, repos_dir)

    # ── Clone ──────────────────────────────────────────────────────────────────
    print("\n--- Clone ---")
    clone_status, clone_times = clone_repos(pairs, logs_dir)

    # ── Checkout ───────────────────────────────────────────────────────────────
    print("\n--- Checkout ---")
    checkout_status, checkout_times = checkout_commits(pairs, clone_status, logs_dir)

    # ── Locate pom + detect Java ───────────────────────────────────────────────
    print("\n--- Pom + Java detection ---")
    for p in pairs:
        key = (p["repo_url"], p["commit_hash"])

        # Propagate clone/checkout failures
        if clone_status.get(key) == "failed":
            p["step1_status"] = "CLONE_FAILED"
            p["clone_sec"]    = clone_times.get(key, 0.0)
            p["checkout_sec"] = 0.0
            continue
        if checkout_status.get(key) == "failed":
            p["step1_status"] = "CHECKOUT_FAILED"
            p["clone_sec"]    = clone_times.get(key, 0.0)
            p["checkout_sec"] = checkout_times.get(key, 0.0)
            continue

        p["clone_sec"]    = clone_times.get(key, 0.0)
        p["checkout_sec"] = checkout_times.get(key, 0.0)

        # pom.xml
        pom = find_pom(p["clone_dir"], p["package"])
        if not pom:
            p["step1_status"]       = "NO_POM_FOUND"
            p["resolved_module_path"] = None
            print(f"  [{p['row_key']}] NO_POM_FOUND  {p['repo_slug']}")
            continue
        p["resolved_module_path"] = pom

        # Java version
        v, assumed = detect_java(p["clone_dir"])
        p["required_java_version"] = v
        p["java_version_assumed"]  = assumed
        flag = " [assumed]" if assumed else ""
        print(f"  [{p['row_key']}] Java {v}{flag}  pom={pom}")

        p["step1_status"] = "READY"

    # ── Write manifest ─────────────────────────────────────────────────────────
    _write_manifest(pairs, manifest_path)

    # ── Summary ────────────────────────────────────────────────────────────────
    counts = {}
    for p in pairs:
        counts[p["step1_status"]] = counts.get(p["step1_status"], 0) + 1
    print(f"\nSTEP 1 SUMMARY: {counts}")
    return pairs


def _write_manifest(pairs, manifest_path):
    os.makedirs(os.path.dirname(manifest_path), exist_ok=True)
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump({"pairs": pairs}, f, indent=2)


# ── Standalone runner ──────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse

    HERE      = os.path.dirname(os.path.abspath(__file__))
    ROOT      = os.path.dirname(os.path.dirname(HERE))  # dataCollection/
    _csv      = os.path.join(ROOT, "openai/data/final_OD_flaky_tests.csv")
    _out      = os.path.join(HERE, "output")
    _manifest = os.path.join(_out, "manifest.json")
    _repos    = os.path.join(ROOT, "repos")
    _logs     = os.path.join(_out, "logs")

    ap = argparse.ArgumentParser()
    ap.add_argument("--input",     default=_csv)
    ap.add_argument("--output-dir",default=_out)
    ap.add_argument("--rows", nargs="*", type=int)
    args = ap.parse_args()

    manifest_path = os.path.join(args.output_dir, "manifest.json")
    logs_dir      = os.path.join(args.output_dir, "logs")

    run_step1(
        csv_path      = args.input,
        manifest_path = manifest_path,
        repos_dir     = _repos,
        logs_dir      = logs_dir,
        target_rows   = set(args.rows) if args.rows else None,
    )
