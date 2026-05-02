#!/usr/bin/env bash
# Run experiment for rows 1-10 (includes reruns for rows 1-5).
# Usage: bash run_experiment_rows1_10.sh
set -euo pipefail

# Load OPENAI_API_KEY from zsh profile if not already set
if [[ -z "${OPENAI_API_KEY:-}" ]]; then
    export OPENAI_API_KEY=$(grep -m1 'export OPENAI_API_KEY=' ~/.zshrc | sed 's/.*OPENAI_API_KEY="\(.*\)"/\1/')
fi

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
LOG="$SCRIPT_DIR/logs/experiment_rows1_10_$(date +%Y%m%d_%H%M%S).log"
mkdir -p "$SCRIPT_DIR/logs"

exec > >(tee -a "$LOG") 2>&1

echo "================================================================"
echo "EXPERIMENT: rows 1-10  (started: $(date))"
echo "================================================================"

cd "$SCRIPT_DIR"

# ── Step 0: Clean rows 1-5 so they rerun ─────────────────────────────────────

echo ""
echo "--- Cleaning rows 1-5 from all sections ---"

# Section 1 patch files
for n in 01 02 03 04 05; do
    rm -f "section1_patches/row${n}__gpt4_1.txt"
    rm -f "section1_patches/row${n}__initial_prompt.txt"
done

# Section 1 metrics: remove row01-row05 keys
python3.9 - <<'PYEOF'
import json, os
mf = "section1_patches/metrics.json"
if os.path.isfile(mf):
    with open(mf) as f:
        m = json.load(f)
    for k in ["row01","row02","row03","row04","row05"]:
        m.pop(k, None)
    with open(mf, "w") as f:
        json.dump(m, f, indent=2)
    print(f"  section1 metrics: remaining keys = {sorted(m.keys())}")
else:
    print("  section1 metrics: file not found, skipping")
PYEOF

# Section 2 parsed dirs
for n in 01 02 03 04 05; do
    rm -rf "section2_parsed/row${n}"
done

# Section 3 baseline files (status + logs)
for n in 01 02 03 04 05; do
    rm -f section3_baseline/row${n}*
done

# Section 4 compilation dirs + metrics
for n in 01 02 03 04 05; do
    rm -rf "section4_compilation/row${n}"
done

python3.9 - <<'PYEOF'
import json, os
mf = "section4_compilation/metrics.json"
if os.path.isfile(mf):
    with open(mf) as f:
        m = json.load(f)
    for k in ["row01","row02","row03","row04","row05"]:
        m.pop(k, None)
    with open(mf, "w") as f:
        json.dump(m, f, indent=2)
    print(f"  section4 metrics: remaining keys = {sorted(m.keys())}")
else:
    print("  section4 metrics: file not found, skipping")
PYEOF

# Section 5 test run dirs
for n in 01 02 03 04 05; do
    rm -rf "section5_test_runs/row${n}"
done

echo "Cleanup complete."

# ── Step 1: Patch generation (rows 1-10) ─────────────────────────────────────

echo ""
echo "================================================================"
echo "SECTION 1: Patch Generation (rows 1-10)"
echo "================================================================"
python3.9 section1_generate_patches.py --start 1 --limit 10

# ── Step 2: Parse patches ─────────────────────────────────────────────────────

echo ""
echo "================================================================"
echo "SECTION 2: Parse Patches"
echo "================================================================"
python3.9 section2_parse_patches.py

# ── Step 3: Apply patches + compile ──────────────────────────────────────────

echo ""
echo "================================================================"
echo "SECTION 4: Compilation + Stitching"
echo "================================================================"
python3.9 section4_compilation.py

# ── Step 4: Test execution ────────────────────────────────────────────────────

echo ""
echo "================================================================"
echo "SECTION 5: Test Execution"
echo "================================================================"
python3.9 section5_test_runs.py

echo ""
echo "================================================================"
echo "ABLATION: No-Repro Variant (rows 1-10)"
echo "================================================================"

# ── Clean rows 1-5 from ablation sections ────────────────────────────────────

echo ""
echo "--- Cleaning ablation rows 1-5 ---"

for n in 01 02 03 04 05; do
    rm -f "section1_patches_ablation/row${n}__gpt4_1.txt"
    rm -f "section1_patches_ablation/row${n}__initial_prompt.txt"
done

python3.9 - <<'PYEOF'
import json, os
mf = "section1_patches_ablation/metrics.json"
if os.path.isfile(mf):
    with open(mf) as f:
        m = json.load(f)
    for k in ["row01","row02","row03","row04","row05"]:
        m.pop(k, None)
    with open(mf, "w") as f:
        json.dump(m, f, indent=2)
    print(f"  section1_ablation metrics: remaining keys = {sorted(m.keys())}")
PYEOF

for n in 01 02 03 04 05; do
    rm -rf "section2_parsed_ablation/row${n}"
    rm -rf "section4_compilation_ablation/row${n}"
    rm -rf "section5_test_runs_ablation/row${n}"
done

python3.9 - <<'PYEOF'
import json, os
mf = "section4_compilation_ablation/metrics.json"
if os.path.isfile(mf):
    with open(mf) as f:
        m = json.load(f)
    for k in ["row01","row02","row03","row04","row05"]:
        m.pop(k, None)
    with open(mf, "w") as f:
        json.dump(m, f, indent=2)
    print(f"  section4_ablation metrics: remaining keys = {sorted(m.keys())}")
PYEOF

echo "No-repro cleanup complete."

# ── Ablation Section 1 ────────────────────────────────────────────────────────

echo ""
echo "================================================================"
echo "ABLATION SECTION 1: Patch Generation (rows 1-10)"
echo "================================================================"
python3.9 section1_generate_patches.py --start 1 --limit 10 --ablation

# ── Ablation Section 2 ────────────────────────────────────────────────────────

echo ""
echo "================================================================"
echo "ABLATION SECTION 2: Parse Patches"
echo "================================================================"
python3.9 section2_parse_patches.py --ablation

# ── Ablation Section 4 ────────────────────────────────────────────────────────

echo ""
echo "================================================================"
echo "ABLATION SECTION 4: Compilation + Stitching"
echo "================================================================"
python3.9 section4_compilation.py --ablation

# ── Ablation Section 5 ────────────────────────────────────────────────────────

echo ""
echo "================================================================"
echo "ABLATION SECTION 5: Test Execution"
echo "================================================================"
python3.9 section5_test_runs.py --ablation

echo ""
echo "================================================================"
echo "EXPERIMENT COMPLETE  (finished: $(date))"
echo "================================================================"
