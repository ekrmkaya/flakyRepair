# Stage 1: Failure Data Collection

Reproduces order-dependent (OD) flaky test failures and extracts structured metadata for downstream patch generation.

## Prerequisites

- Python 3.9+
- Java 8+ (set `JAVA8_HOME` / `JAVA11_HOME` / `JAVA17_HOME` if not on macOS default paths)
- Maven 3.5+
- Python dependencies: `pip install -r requirements.txt`

## Usage

```bash
# Run all 50 rows
python3.9 pipeline.py

# Run specific rows
python3.9 pipeline.py --rows 1 2 3
python3.9 pipeline.py --rows 1-10

# Force overwrite existing results
python3.9 pipeline.py --rows 1-10 --force

# Skip clone/checkout (repos already present)
python3.9 pipeline.py --rows 1-10 --skip-step1
```

## Input

`../data/final_OD_flaky_tests.csv` -- 50 OD flaky test pairs with repo URLs, commit hashes, victim/polluter test names.

## Output

All outputs are written to `output/` (gitignored):

| File | Description |
|------|-------------|
| `manifest.json` | Execution state for all rows (resumable) |
| `flaky_test_data.json` | Extracted test metadata for Stage 2 |
| `row{N}_metadata.json` | Per-row extracted metadata |
| `timing_report.json` | Performance metrics |
| `logs/` | Step-by-step execution logs |

## Pipeline Steps

1. **step1_setup.py** -- Clone repos, checkout target commits, detect Java versions, locate pom.xml files.
2. **step2_prebuild.py** -- Pre-compile all repos (`mvn test-compile`) to warm Maven caches.
3. **step3_reproduce.py** -- Two-phase reproduction: (A) victim passes alone, (B) victim fails after polluter.
4. **step4_extract.py** -- Extract test source code, error messages, failing lines, helper methods, and global variables.
5. **step5_assemble.py** -- Assemble `flaky_test_data.json` and timing report from reproduced rows.

## Notes

- The pipeline is resumable: re-running skips completed steps based on `manifest.json` status fields.
- Cloned repos are placed in `../repos/` (one level up, at the repository root).
- Some repos require offline Maven builds (dependency jars are pre-cached during step 2).
