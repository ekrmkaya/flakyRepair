# No-API Validation

Validates pre-computed GPT-4 patches **without an OpenAI API key**. Compiles each existing patch, runs the OD test (polluter then victim), and checks isolation — all without making any GPT-4 API calls.

## Prerequisites

- Python 3.9+
- Java 8+ (set `JAVA8_HOME`, `JAVA11_HOME`, etc. if not on macOS default paths)
- Maven 3.5+
- `pip install -r ../openai_patch_evaluation/requirements.txt`

No API key is needed.

## Usage

```bash
cd no_api_validation

# Validate all 50 rows (both with_repro and no_repro conditions)
python3.9 validate_patches.py

# Validate specific rows
python3.9 validate_patches.py --rows 6 19 23

# Validate a range
python3.9 validate_patches.py --rows 1-10

# Skip repo clone/build checks (if repos are already set up)
python3.9 validate_patches.py --skip-preflight
```

## What It Does

1. **Checks patch availability** for each requested row before any repo work.
2. **Clones and builds** any missing repos automatically (from `data/final_OD_flaky_tests.csv`).
3. **Finds the best available patch** for each row by searching (in priority order):
   - Section 5 test run attempts (most recent first)
   - Section 4 compiled patches (stitched or initial)
   - Section 2 parsed fix code (rebuilt from scratch)
4. **Compiles and tests** each patch (single attempt, no retries, no GPT-4 calls).
5. **Categorizes outcomes** and writes a results CSV.
6. **Cleans up** intermediate files, keeping only the final CSV.

## Output

Results are written to `validation_results/results.csv` with these columns:

| Column | Description |
|--------|-------------|
| `test_id`, `row_num`, `condition` | Row identity |
| `victim`, `polluter` | Test pair names |
| `repo_url`, `commit` | Source repository |
| `final_status` | `FIXED`, `NOT_FIXED`, or `COMPILE_ERROR` |
| `category` | Categorization (e.g., "Fixed flakiness", "Compilation error") |
| `passed` | `Y` or `N` |
| `validation_patch_source` | Which patch was used (e.g., `s5_attempt1`, `s4_initial`) |
| `validation_compile_sec` | Compile time during validation |
| `validation_test_sec` | OD test time |
| `validation_victim_alone_sec` | Victim isolation check time |
| `validation_polluter_alone_sec` | Polluter isolation check time |
| `validation_total_sec` | Total validation time |
| `original_attempts` | Number of attempts in the original GPT-4 run |
| `original_total_tokens` | Total GPT-4 tokens used in the original run |
| `original_total_sec` | Total time of the original run |

A `skip_log.csv` is also written if any rows were skipped, with the reason for each.

## Notes

- This tool reads from the pre-computed outputs in `openai_patch_evaluation/` (sections 1-5) but **never modifies them**.
- Repos are cloned to `repos/` at the project root (same location as the full pipeline).
- Re-running clears previous validation results automatically.
