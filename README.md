# FlakyRepair: Automated Repair of Order-Dependent Flaky Tests Using GPT-4

This repository contains the implementation and evaluation pipeline for automated repair of order-dependent (OD) flaky tests using GPT-4. The system identifies shared-state pollution between tests, generates targeted patches, and evaluates whether the patches compile and fix the flakiness.

## Pipeline Overview

The pipeline has two stages:

```
data/final_OD_flaky_tests.csv
    |
    v
[Stage 1] failure_data_collection/pipeline.py
    - Clones repos, detects Java versions, compiles
    - Reproduces OD failures (victim passes alone, fails after polluter)
    - Extracts test code, error messages, and metadata
    |
    v
failure_data_collection/output/manifest.json + flaky_test_data.json
    |
    v
[Stage 2] openai_patch_evaluation/run_experiment.sh
    - Prompts GPT-4 for patches (with and without reproduction steps)
    - Parses, compiles, and stitches patches into test files
    - Runs OD tests with retry loop (up to 5 attempts)
    - Categorizes outcomes and assembles results CSV
    |
    v
openai_patch_evaluation/section7_results/results.csv
```

## Prerequisites

- **Python 3.9+**
- **Java 8+** (auto-detected; set `JAVA8_HOME`, `JAVA11_HOME`, etc. if not on macOS default paths)
- **Maven 3.5+**
- **OpenAI API key** (`export OPENAI_API_KEY=sk-...`)

## Pre-computed Results

This repository ships with pre-computed results for all 50 rows (both with_repro and no_repro variants), since the full pipeline for all 50 rows can take over 3 hours. You can inspect them directly without running anything:

- `failure_data_collection/output/` -- Stage 1 reproduction data and logs
- `openai_patch_evaluation/section1_patches/` through `section7_results/` -- Stage 2 patch evaluation outputs

Running the pipeline will overwrite results for the targeted rows.

## Quick Demo

To verify the pipeline works end-to-end, run the demo script on 3 small, fast rows (6, 19, 23) that span three different projects (http-request, visualee, wikidata-toolkit):

```bash
# 1. Clone
git clone <repo-url> && cd flakyRepair

# 2. Install Python dependencies
bash install.sh

# 3. Set your OpenAI API key
export OPENAI_API_KEY="sk-..."

# 4. Run the demo (rows 6, 19, 23 — takes ~5 minutes)
bash run_demo.sh
```

This runs both Stage 1 (data collection) and Stage 2 (patch evaluation) for the 3 demo rows. Results for rows 6, 19, and 23 will be overwritten; all other rows remain unchanged.

## Full Pipeline

To run the full pipeline on all 50 rows (or a custom subset):

```bash
# Set your OpenAI API key
export OPENAI_API_KEY="sk-..."

# Run Stage 1 — reproduce OD flaky tests
cd failure_data_collection
python3.9 pipeline.py --rows 1-10      # or omit --rows for all 50

# Run Stage 2 — generate and evaluate GPT-4 patches
cd ../openai_patch_evaluation
bash run_experiment.sh 1 2 3 4 5 6 7 8 9 10   # or omit args for all 50
```

## Repository Structure

```
flakyRepair/
├── README.md                          Project documentation
├── install.sh                         Installs Python dependencies for both stages
├── run_demo.sh                        Demo script: runs full pipeline on 3 fast rows (6, 19, 23)
├── data/                              Input datasets (CSV)
│   └── final_OD_flaky_tests.csv       50 OD flaky test pairs
├── failure_data_collection/           Stage 1: reproduce and extract
│   ├── README.md                      Stage 1 documentation
│   ├── pipeline.py                    Orchestrator (steps 1-5)
│   ├── config.py                      Java paths, timeouts, strategies
│   ├── step1_setup.py                 Clone, checkout, Java detection
│   ├── step2_prebuild.py              Pre-compile repos
│   ├── step3_reproduce.py             Two-phase OD reproduction
│   ├── step4_extract.py               Code + error extraction
│   ├── step5_assemble.py              JSON assembly
│   ├── repro_strategies.py            Repo-specific reproduction logic
│   ├── requirements.txt               Python dependencies for Stage 1
│   └── output/                        Generated (gitignored)
├── openai_patch_evaluation/           Stage 2: GPT-4 patch evaluation
│   ├── README.md                      Stage 2 documentation
│   ├── run_experiment.sh              Full pipeline runner
│   ├── section1_generate_patches.py   GPT-4 prompting
│   ├── section2_parse_patches.py      Parse fix/import/pom blocks
│   ├── section4_compilation.py        Apply patches + compile + stitch
│   ├── section5_test_runs.py          OD test execution with retries
│   ├── section6_categorize.py         Outcome categorization
│   ├── section7_assemble_csv.py       Final CSV assembly
│   ├── test_execution.py              Shared test execution utilities
│   ├── requirements.txt               Python dependencies for Stage 2
│   ├── logs/                          Pipeline execution logs (gitignored)
│   └── section7_results/              Generated (gitignored)
└── repos/                             Cloned Java projects (gitignored)
```

## Notes

- `repos/` is created at runtime by Stage 1 (cloned from GitHub). It is not committed.
- Pre-computed results for all 50 rows are included in the repository. Running the pipeline overwrites results for the targeted rows only.
- The evaluation runs two variants per row: **with_repro** (includes reproduction steps in the prompt) and **no_repro** (excludes them), enabling comparison of prompt strategies.
