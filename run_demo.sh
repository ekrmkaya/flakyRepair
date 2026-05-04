#!/usr/bin/env bash
# Run the full FlakyRepair pipeline (Stage 1 + Stage 2) for 3 small demo rows.
#
# Rows: 6 (http-request), 19 (visualee), 23 (wikidata-toolkit)
# These were chosen because they are fast (~35-40s each) and were successfully
# fixed by GPT-4 in both with_repro and no_repro variants.
#
# NOTE: The repository ships with pre-computed results for all 50 rows.
#       Running this demo will overwrite results for rows 6, 19, and 23.
#
# Usage:
#   bash run_demo.sh
#
# Requires: OPENAI_API_KEY environment variable, Python 3.9, Java 8+, Maven.

set -euo pipefail

DEMO_ROWS=(6 19 23)

# ── API key ───────────────────────────────────────────────────────────────────
if [[ -z "${OPENAI_API_KEY:-}" ]]; then
    echo "ERROR: OPENAI_API_KEY is not set. Export it before running this script."
    exit 1
fi

REPO_ROOT="$(cd "$(dirname "$0")" && pwd)"
export REPO_ROOT

# ── Clean previous demo outputs ─────────────────────────────────────────────
echo "Cleaning previous outputs for demo rows: ${DEMO_ROWS[*]} ..."

for ROW in "${DEMO_ROWS[@]}"; do
    ROWKEY=$(printf "row%02d" "$ROW")

    # failure_data_collection: logs and metadata
    rm -f "$REPO_ROOT/failure_data_collection/output/${ROWKEY}_metadata.json"
    rm -f "$REPO_ROOT/failure_data_collection/output/logs/${ROWKEY}_phaseA.log"
    rm -f "$REPO_ROOT/failure_data_collection/output/logs/${ROWKEY}_phaseB.log"

    # openai_patch_evaluation: per-row files across all section dirs
    rm -f "$REPO_ROOT/openai_patch_evaluation/section1_patches/${ROWKEY}__"*.txt
    rm -f "$REPO_ROOT/openai_patch_evaluation/section1_patches_no_repro/${ROWKEY}__"*.txt
    rm -rf "$REPO_ROOT/openai_patch_evaluation/section2_parsed/${ROWKEY}"
    rm -rf "$REPO_ROOT/openai_patch_evaluation/section2_parsed_no_repro/${ROWKEY}"
    rm -rf "$REPO_ROOT/openai_patch_evaluation/section4_compilation/${ROWKEY}"
    rm -rf "$REPO_ROOT/openai_patch_evaluation/section4_compilation_no_repro/${ROWKEY}"
    rm -rf "$REPO_ROOT/openai_patch_evaluation/section6_categories/${ROWKEY}_with_repro"
    rm -rf "$REPO_ROOT/openai_patch_evaluation/section6_categories/${ROWKEY}_no_repro"
done

# Remove demo-row entries from shared JSON/CSV files
python3.9 - "${DEMO_ROWS[@]}" <<'PYSCRIPT'
import json, csv, sys, os

repo_root = os.environ["REPO_ROOT"]
demo_rows = [int(r) for r in sys.argv[1:]]
row_keys = [f"row{r:02d}" for r in demo_rows]

def load_json(path):
    if not os.path.isfile(path):
        return None
    with open(path) as f:
        return json.load(f)

def save_json(path, data):
    with open(path, "w") as f:
        json.dump(data, f, indent=2)
        f.write("\n")

# --- failure_data_collection/output/manifest.json ---
fdc = os.path.join(repo_root, "failure_data_collection", "output")
manifest = load_json(os.path.join(fdc, "manifest.json"))
if manifest and "pairs" in manifest:
    manifest["pairs"] = [p for p in manifest["pairs"] if p.get("row_key") not in row_keys]
    save_json(os.path.join(fdc, "manifest.json"), manifest)

# --- failure_data_collection/output/flaky_test_data.json ---
# Entries are identified by source URL; get URLs from manifest
repo_urls = set()
orig_manifest = load_json(os.path.join(fdc, "manifest.json"))
# We already removed them from manifest, so use the CSV or just match by known slugs
# Simpler: match by known repo slugs from DEMO_ROWS
slug_map = {}
full_manifest_path = os.path.join(fdc, "manifest.json")
# Re-read original manifest from git to get the URLs
import subprocess
try:
    orig = subprocess.check_output(
        ["git", "show", "HEAD:failure_data_collection/output/manifest.json"],
        cwd=repo_root, stderr=subprocess.DEVNULL
    )
    orig_manifest_data = json.loads(orig)
    for p in orig_manifest_data.get("pairs", []):
        if p.get("row_key") in row_keys:
            repo_urls.add(p.get("repo_url", ""))
            slug_map[p["row_key"]] = p.get("repo_slug", "")
except Exception:
    # Fallback: known slugs for demo rows
    pass

ftd_path = os.path.join(fdc, "flaky_test_data.json")
ftd = load_json(ftd_path)
if ftd and "testdata" in ftd and repo_urls:
    ftd["testdata"] = [e for e in ftd["testdata"] if e.get("source", "") not in repo_urls]
    save_json(ftd_path, ftd)

# --- failure_data_collection/output/timing_report.json ---
tr_path = os.path.join(fdc, "timing_report.json")
tr = load_json(tr_path)
if tr:
    for rk in row_keys:
        tr.pop(rk, None)
    save_json(tr_path, tr)

# --- openai_patch_evaluation metrics.json files ---
ope = os.path.join(repo_root, "openai_patch_evaluation")
for mdir in ["section1_patches", "section1_patches_no_repro",
             "section5_test_runs", "section5_test_runs_no_repro"]:
    mpath = os.path.join(ope, mdir, "metrics.json")
    mdata = load_json(mpath)
    if mdata:
        for rk in row_keys:
            mdata.pop(rk, None)
        save_json(mpath, mdata)

# --- openai_patch_evaluation/section7_results/results.csv & summary.csv ---
for csv_name in ["results.csv", "summary.csv"]:
    csv_path = os.path.join(ope, "section7_results", csv_name)
    if not os.path.isfile(csv_path):
        continue
    with open(csv_path, newline="") as f:
        reader = csv.reader(f)
        rows_out = []
        for i, row in enumerate(reader):
            if i == 0:
                rows_out.append(row)  # header
                continue
            # row_num is the second column
            try:
                if int(row[1]) in demo_rows:
                    continue
            except (IndexError, ValueError):
                pass
            rows_out.append(row)
    with open(csv_path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerows(rows_out)

print(f"Cleaned demo-row entries from shared output files.")
PYSCRIPT

echo "Clean-up complete."

echo "================================================================"
echo "DEMO: FlakyRepair end-to-end pipeline"
echo "Rows: ${DEMO_ROWS[*]}"
echo "Started: $(date)"
echo "================================================================"

# ── Stage 1: Failure Data Collection ─────────────────────────────────────────
echo ""
echo "================================================================"
echo "STAGE 1: Failure Data Collection (rows ${DEMO_ROWS[*]})"
echo "================================================================"
cd "$REPO_ROOT/failure_data_collection"
python3.9 pipeline.py --rows "${DEMO_ROWS[@]}" --force

# ── Determine which rows reproduced successfully ────────────────────────────
REPRO_ROWS=()
for ROW in "${DEMO_ROWS[@]}"; do
    ROWKEY=$(printf "row%02d" "$ROW")
    STATUS=$(python3.9 -c "
import json
m = json.load(open('$REPO_ROOT/failure_data_collection/output/manifest.json'))
for p in m['pairs']:
    if p['row_key'] == '$ROWKEY':
        print(p.get('step3_status',''))
        break
")
    if [[ "$STATUS" == "REPRODUCED" ]]; then
        REPRO_ROWS+=("$ROW")
    else
        echo "  [SKIP] Row $ROW: step3_status=$STATUS (not reproduced)"
    fi
done

if [[ ${#REPRO_ROWS[@]} -eq 0 ]]; then
    echo "ERROR: No rows reproduced successfully. Cannot proceed to Stage 2."
    exit 1
fi

# ── Stage 2: GPT-4 Patch Evaluation ─────────────────────────────────────────
echo ""
echo "================================================================"
echo "STAGE 2: GPT-4 Patch Evaluation (rows ${REPRO_ROWS[*]})"
echo "================================================================"
cd "$REPO_ROOT/openai_patch_evaluation"
bash run_experiment.sh "${REPRO_ROWS[@]}"

echo ""
echo "================================================================"
echo "DEMO COMPLETE  (finished: $(date))"
echo "Results: openai_patch_evaluation/section7_results/results.csv"
echo "================================================================"
