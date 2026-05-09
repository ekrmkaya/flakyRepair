# Stage 2: GPT-4 Patch Evaluation

Generates GPT-4 patches for OD flaky tests, compiles them, runs OD reproduction tests, and categorizes the outcomes. Runs two variants: **with_repro** (reproduction steps included in prompt) and **no_repro** (excluded).

## Prerequisites

- Python 3.9+, Java 8+, Maven 3.5+
- `pip install -r requirements.txt`
- `export OPENAI_API_KEY="sk-..."` — required for generating new patches
- Stage 1 must be completed first (`failure_data_collection/pipeline.py`)

> **No API key?** Pre-computed results for all 50 rows are included in this repository. To validate them without an API key, use the no-API validation tool in `../no_api_validation/` instead. See the [project README](../README.md#no-api-validation) for details.

## Usage

```bash
# Run full pipeline for all 50 rows (both variants)
bash run_experiment.sh

# Run specific rows
bash run_experiment.sh 1 5 10 23

# Run individual sections manually
python3.9 section1_generate_patches.py --rows 1-10
python3.9 section2_parse_patches.py --only row01
python3.9 section4_compilation.py --only row01
python3.9 section5_test_runs.py --only row01
python3.9 section6_categorize.py --only row01
python3.9 section7_assemble_csv.py

# Run the no-repro variant for a section
python3.9 section1_generate_patches.py --rows 1-10 --no-repro
```

## Input

Reads from Stage 1 output:
- `../failure_data_collection/output/manifest.json` -- target metadata (repos, commits, Java versions)
- `../failure_data_collection/output/flaky_test_data.json` -- test code, error messages, reproduction steps

## Pipeline Sections

### Section 1 -- Patch Generation (`section1_generate_patches.py`)
Prompts GPT-4 (`temperature=0.2`) with test metadata, error info, and code context. Writes raw responses to `section1_patches/`.

### Section 2 -- Parse Patches (`section2_parse_patches.py`)
Extracts structured blocks from GPT-4 output: fix code (`//<fix start>`), imports (`//<import start>`), and pom dependencies. Writes to `section2_parsed/row{N}/`.

### Section 4 -- Compilation + Stitching (`section4_compilation.py`)
Applies parsed patches to the original test file, compiles with Maven. On failure, calls GPT-4 to "stitch" the fix into the full file context. Writes to `section4_compilation/row{N}/`.

### Section 5 -- Test Execution (`section5_test_runs.py`)
Runs the patched OD test (polluter then victim) with up to 5 retry attempts. On compile failure, asks GPT-4 for a corrected patch with the compiler errors included. On test failure, asks GPT-4 for an alternative fix with the test output included. Writes to `section5_test_runs/row{N}/`.

### Section 6 -- Categorization (`section6_categorize.py`)
Categorizes each row's outcome: "Fixed flakiness", "Compilation error", "Did not address flakiness", etc. Writes to `section6_categories/`.

### Section 7 -- Results Assembly (`section7_assemble_csv.py`)
Aggregates all sections into `section7_results/results.csv` (one row per test x condition) and `section7_results/summary.csv` (side-by-side comparison of with_repro vs no_repro).

### Supporting Module -- `test_execution.py`
Shared OD test execution utilities used by multiple pipeline sections: loads target metadata from the Stage 1 manifest, runs OD tests by dispatching to the correct reproduction strategy, interprets test results for patch evaluation, and manages JDK environment setup.

## Output Structure

```
section1_patches/           Raw GPT-4 responses + metrics.json
section2_parsed/row{N}/     fix_code.java, imports.txt, pom_snippet.xml
section4_compilation/row{N}/ patched_test.java, compile.log, stitched_test.java
section5_test_runs/row{N}/  row_result.json, attempt{N}/attempt.json
section6_categories/        category.txt, passed.txt per row x condition
section7_results/           results.csv, summary.csv
logs/                       Pipeline execution logs from run_experiment.sh
```

Pre-computed results for all 50 rows are included in the repository, since the full pipeline for all 50 rows can take over 3 hours. Running the pipeline will overwrite results for the targeted rows only.

## Variants

| Variant | CLI Flag | Description |
|---------|----------|-------------|
| **with_repro** | *(default)* | Prompt includes reproduction steps |
| **no_repro** | `--no-repro` | Prompt excludes reproduction steps |

The `run_experiment.sh` script runs both variants sequentially, then categorizes and assembles results.
