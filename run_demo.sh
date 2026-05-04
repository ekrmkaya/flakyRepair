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

# ── Stage 2: GPT-4 Patch Evaluation ─────────────────────────────────────────
echo ""
echo "================================================================"
echo "STAGE 2: GPT-4 Patch Evaluation (rows ${DEMO_ROWS[*]})"
echo "================================================================"
cd "$REPO_ROOT/openai_patch_evaluation"
bash run_experiment.sh "${DEMO_ROWS[@]}"

echo ""
echo "================================================================"
echo "DEMO COMPLETE  (finished: $(date))"
echo "Results: openai_patch_evaluation/section7_results/results.csv"
echo "================================================================"
