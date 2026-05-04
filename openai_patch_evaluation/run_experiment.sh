#!/usr/bin/env bash
# Run the full GPT-4 patch evaluation pipeline for specified rows.
#
# Usage:
#   bash run_experiment.sh              # runs all rows 1–50
#   bash run_experiment.sh 1 5 10 23   # runs only the listed rows
#
# Requires: OPENAI_API_KEY environment variable, Python 3.9, Java 8+, Maven.

set -euo pipefail

# ── API key ───────────────────────────────────────────────────────────────────
if [[ -z "${OPENAI_API_KEY:-}" ]]; then
    echo "ERROR: OPENAI_API_KEY is not set. Export it before running this script."
    exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

# ── Parse row arguments (default: 1–50) ──────────────────────────────────────
if [[ $# -eq 0 ]]; then
    ROWS=()
    for i in $(seq 1 50); do ROWS+=("$i"); done
else
    ROWS=("$@")
fi

# Zero-pad to row key: 1 → row01, 12 → row12
row_key() { printf "row%02d" "$1"; }

# ── Detect rows that already have output ─────────────────────────────────────
EXISTING_ROWS=()
for n in "${ROWS[@]}"; do
    key=$(row_key "$n")
    if [[ -f "section1_patches/${key}__gpt4_1.txt"           || \
          -d "section2_parsed/${key}"                         || \
          -d "section4_compilation/${key}"                    || \
          -d "section5_test_runs/${key}"                      || \
          -d "section6_categories/${key}_with_repro"          || \
          -f "section1_patches_no_repro/${key}__gpt4_1.txt"   || \
          -d "section2_parsed_no_repro/${key}"                || \
          -d "section4_compilation_no_repro/${key}"           || \
          -d "section5_test_runs_no_repro/${key}"             || \
          -d "section6_categories/${key}_no_repro" ]]; then
        EXISTING_ROWS+=("$n")
    fi
done

# ── Confirm overwrite if needed ───────────────────────────────────────────────
if [[ ${#EXISTING_ROWS[@]} -gt 0 ]]; then
    echo "Existing results found for row(s): ${EXISTING_ROWS[*]}"
    read -r -p "Overwrite existing results? [y/N] " answer
    case "$answer" in
        [yY][eE][sS]|[yY]) ;;
        *)
            echo "Aborted. No scripts were run."
            exit 0
            ;;
    esac
fi

# ── Set up logging ────────────────────────────────────────────────────────────
ROW_LABEL=$(IFS=_; echo "${ROWS[*]}")
if [[ ${#ROWS[@]} -gt 6 ]]; then
    ROW_LABEL="${ROWS[0]}-${ROWS[${#ROWS[@]}-1]}_n${#ROWS[@]}"
fi
LOG="$SCRIPT_DIR/logs/experiment_rows_${ROW_LABEL}_$(date +%Y%m%d_%H%M%S).log"
mkdir -p "$SCRIPT_DIR/logs"
exec > >(tee -a "$LOG") 2>&1

echo "================================================================"
echo "EXPERIMENT: rows ${ROWS[*]}  (started: $(date))"
echo "================================================================"

# ── Clean existing results for target rows ────────────────────────────────────
clean_row() {
    local n=$1
    local key
    key=$(row_key "$n")

    # Main variant
    rm -f  "section1_patches/${key}__gpt4_1.txt"
    rm -f  "section1_patches/${key}__initial_prompt.txt"
    rm -rf "section2_parsed/${key}"
    rm -rf "section4_compilation/${key}"
    rm -rf "section5_test_runs/${key}"
    rm -rf "section6_categories/${key}_with_repro"

    # No-repro variant
    rm -f  "section1_patches_no_repro/${key}__gpt4_1.txt"
    rm -f  "section1_patches_no_repro/${key}__initial_prompt.txt"
    rm -rf "section2_parsed_no_repro/${key}"
    rm -rf "section4_compilation_no_repro/${key}"
    rm -rf "section5_test_runs_no_repro/${key}"
    rm -rf "section6_categories/${key}_no_repro"
}

# Remove a list of row keys from a JSON metrics file
remove_metrics_keys() {
    local mf="$1"; shift
    local keys_json
    keys_json=$(printf '"%s",' "$@" | sed 's/,$//')
    python3.9 - <<PYEOF
import json, os
mf = "$mf"
keys = [$keys_json]
if os.path.isfile(mf):
    with open(mf) as f:
        m = json.load(f)
    for k in keys:
        m.pop(k, None)
    with open(mf, "w") as f:
        json.dump(m, f, indent=2)
    print(f"  {mf}: remaining keys = {sorted(m.keys())}")
else:
    print(f"  {mf}: not found, skipping")
PYEOF
}

if [[ ${#EXISTING_ROWS[@]} -gt 0 ]]; then
    echo ""
    echo "--- Cleaning rows: ${EXISTING_ROWS[*]} ---"

    METRIC_KEYS=()
    for n in "${EXISTING_ROWS[@]}"; do
        clean_row "$n"
        METRIC_KEYS+=("$(row_key "$n")")
    done

    remove_metrics_keys "section1_patches/metrics.json"            "${METRIC_KEYS[@]}"
    remove_metrics_keys "section4_compilation/metrics.json"        "${METRIC_KEYS[@]}"
    remove_metrics_keys "section1_patches_no_repro/metrics.json"   "${METRIC_KEYS[@]}"
    remove_metrics_keys "section4_compilation_no_repro/metrics.json" "${METRIC_KEYS[@]}"

    echo "Cleanup complete."
fi

# ── MAIN VARIANT (with reproduction steps) ───────────────────────────────────

echo ""
echo "================================================================"
echo "SECTION 1: Patch Generation (with_repro)"
echo "================================================================"
for n in "${ROWS[@]}"; do
    echo "  → row $n"
    python3.9 section1_generate_patches.py --start "$n" --limit 1
done

echo ""
echo "================================================================"
echo "SECTION 2: Parse Patches (with_repro)"
echo "================================================================"
for n in "${ROWS[@]}"; do
    key=$(row_key "$n")
    echo "  → $key"
    python3.9 section2_parse_patches.py --only "$key"
done

echo ""
echo "================================================================"
echo "SECTION 4: Compilation + Stitching (with_repro)"
echo "================================================================"
for n in "${ROWS[@]}"; do
    key=$(row_key "$n")
    echo "  → $key"
    python3.9 section4_compilation.py --only "$key"
done

echo ""
echo "================================================================"
echo "SECTION 5: Test Execution (with_repro)"
echo "================================================================"
for n in "${ROWS[@]}"; do
    key=$(row_key "$n")
    echo "  → $key"
    python3.9 section5_test_runs.py --only "$key"
done

# ── NO-REPRO VARIANT (without reproduction steps) ───────────────────────────

echo ""
echo "================================================================"
echo "SECTION 1: Patch Generation (no_repro)"
echo "================================================================"
for n in "${ROWS[@]}"; do
    echo "  → row $n"
    python3.9 section1_generate_patches.py --start "$n" --limit 1 --no-repro
done

echo ""
echo "================================================================"
echo "SECTION 2: Parse Patches (no_repro)"
echo "================================================================"
for n in "${ROWS[@]}"; do
    key=$(row_key "$n")
    echo "  → $key"
    python3.9 section2_parse_patches.py --only "$key" --no-repro
done

echo ""
echo "================================================================"
echo "SECTION 4: Compilation + Stitching (no_repro)"
echo "================================================================"
for n in "${ROWS[@]}"; do
    key=$(row_key "$n")
    echo "  → $key"
    python3.9 section4_compilation.py --only "$key" --no-repro
done

echo ""
echo "================================================================"
echo "SECTION 5: Test Execution (no_repro)"
echo "================================================================"
for n in "${ROWS[@]}"; do
    key=$(row_key "$n")
    echo "  → $key"
    python3.9 section5_test_runs.py --only "$key" --no-repro
done

# ── CATEGORIZATION + CSV ASSEMBLY ────────────────────────────────────────────

echo ""
echo "================================================================"
echo "SECTION 6: Categorization (both variants)"
echo "================================================================"
for n in "${ROWS[@]}"; do
    key=$(row_key "$n")
    echo "  → $key"
    python3.9 section6_categorize.py --only "$key"
done

echo ""
echo "================================================================"
echo "SECTION 7: Assemble Results CSV"
echo "================================================================"
python3.9 section7_assemble_csv.py

echo ""
echo "================================================================"
echo "EXPERIMENT COMPLETE  (finished: $(date))"
echo "================================================================"
