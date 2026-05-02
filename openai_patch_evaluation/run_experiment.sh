#!/usr/bin/env bash
# Run patch-generation experiment for specified rows (default: all 1–30).
#
# Usage:
#   bash run_experiment.sh              # runs all rows 1–30
#   bash run_experiment.sh 1 5 10 23   # runs only the listed rows
#
# The script checks for existing results before running and prompts for
# confirmation to overwrite. If the user declines, the script exits without
# running anything.

set -euo pipefail

# ── API key ───────────────────────────────────────────────────────────────────
if [[ -z "${OPENAI_API_KEY:-}" ]]; then
    export OPENAI_API_KEY=$(grep -m1 'export OPENAI_API_KEY=' ~/.zshrc | sed 's/.*OPENAI_API_KEY="\(.*\)"/\1/')
fi

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

# ── Parse row arguments (default: 1–50) ──────────────────────────────────────
if [[ $# -eq 0 ]]; then
    mapfile -t ROWS < <(seq 1 30)
else
    ROWS=("$@")
fi

# Zero-pad to row key: 1 → row01, 12 → row12
row_key() { printf "row%02d" "$1"; }

# ── Detect rows that already have output ─────────────────────────────────────
EXISTING_ROWS=()
for n in "${ROWS[@]}"; do
    key=$(row_key "$n")
    if [[ -f "section1_patches/${key}__gpt4_1.txt"         || \
          -d "section2_parsed/${key}"                       || \
          -d "section4_compilation/${key}"                  || \
          -d "section5_test_runs/${key}"                    || \
          -f "section1_patches_ablation/${key}__gpt4_1.txt" || \
          -d "section2_parsed_ablation/${key}"              || \
          -d "section4_compilation_ablation/${key}"         || \
          -d "section5_test_runs_ablation/${key}" ]]; then
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
    ROW_LABEL="${ROWS[0]}-${ROWS[-1]}_n${#ROWS[@]}"
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

    # No-repro variant
    rm -f  "section1_patches_ablation/${key}__gpt4_1.txt"
    rm -f  "section1_patches_ablation/${key}__initial_prompt.txt"
    rm -rf "section2_parsed_ablation/${key}"
    rm -rf "section4_compilation_ablation/${key}"
    rm -rf "section5_test_runs_ablation/${key}"
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

    remove_metrics_keys "section1_patches/metrics.json"          "${METRIC_KEYS[@]}"
    remove_metrics_keys "section4_compilation/metrics.json"      "${METRIC_KEYS[@]}"
    remove_metrics_keys "section1_patches_ablation/metrics.json" "${METRIC_KEYS[@]}"
    remove_metrics_keys "section4_compilation_ablation/metrics.json" "${METRIC_KEYS[@]}"

    echo "Cleanup complete."
fi

# ── MAIN VARIANT ──────────────────────────────────────────────────────────────

echo ""
echo "================================================================"
echo "SECTION 1: Patch Generation (main)"
echo "================================================================"
for n in "${ROWS[@]}"; do
    echo "  → row $n"
    python3.9 section1_generate_patches.py --start "$n" --limit 1
done

echo ""
echo "================================================================"
echo "SECTION 2: Parse Patches (main)"
echo "================================================================"
for n in "${ROWS[@]}"; do
    key=$(row_key "$n")
    echo "  → $key"
    python3.9 section2_parse_patches.py --only "$key"
done

echo ""
echo "================================================================"
echo "SECTION 4: Compilation + Stitching (main)"
echo "================================================================"
for n in "${ROWS[@]}"; do
    key=$(row_key "$n")
    echo "  → $key"
    python3.9 section4_compilation.py --only "$key"
done

echo ""
echo "================================================================"
echo "SECTION 5: Test Execution (main)"
echo "================================================================"
for n in "${ROWS[@]}"; do
    key=$(row_key "$n")
    echo "  → $key"
    python3.9 section5_test_runs.py --only "$key"
done

# ── ABLATION ─────────────────────────────────────────────────────────────────

echo ""
echo "================================================================"
echo "ABLATION SECTION 1: Patch Generation (ablation)"
echo "================================================================"
for n in "${ROWS[@]}"; do
    echo "  → row $n"
    python3.9 section1_generate_patches.py --start "$n" --limit 1 --ablation
done

echo ""
echo "================================================================"
echo "ABLATION SECTION 2: Parse Patches (ablation)"
echo "================================================================"
for n in "${ROWS[@]}"; do
    key=$(row_key "$n")
    echo "  → $key"
    python3.9 section2_parse_patches.py --only "$key" --ablation
done

echo ""
echo "================================================================"
echo "ABLATION SECTION 4: Compilation + Stitching (ablation)"
echo "================================================================"
for n in "${ROWS[@]}"; do
    key=$(row_key "$n")
    echo "  → $key"
    python3.9 section4_compilation.py --only "$key" --ablation
done

echo ""
echo "================================================================"
echo "ABLATION SECTION 5: Test Execution (ablation)"
echo "================================================================"
for n in "${ROWS[@]}"; do
    key=$(row_key "$n")
    echo "  → $key"
    python3.9 section5_test_runs.py --only "$key" --ablation
done

echo ""
echo "================================================================"
echo "EXPERIMENT COMPLETE  (finished: $(date))"
echo "================================================================"
