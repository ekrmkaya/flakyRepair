# openai_patch_evaluation — Pipeline README

## Purpose
Parallel evaluation pipeline using GPT-4 (instead of Claude) to generate and assess
patches for order-dependent (OD) flaky tests. Mirrors the structure of `../patch_evaluation/`
but uses the OpenAI API and may introduce different prompting strategies.

---

## Input Data
| File | Description |
|------|-------------|
| `flaky_test_data_no_suspect.json` | 50 unique OD flaky test entries — all fields from `../flaky_test_data.json` except `suspect_lines` |

### Entry fields
`od_or_id`, `source`, `reproduction_steps`, `victim_test_name`, `polluter_test_name`,
`error_messages`, `failing_lines`, `global_variables`, `helper_methods`, `full_test_code`

> `global_variables` and `helper_methods` may be empty strings — that is expected.
> Entry 44 (`Slf4JLoggerTest.testLogger`) has an intentionally empty `error_messages`.

---

## Directory Layout
```
openai_patch_evaluation/
├── README.md                        ← this file
├── flaky_test_data_no_suspect.json  ← 50-entry input dataset
├── logs/                            ← runtime logs
├── section1_generate_patches.py     ← GPT-4 prompting → section1_patches/
├── section1_patches/                ← raw GPT-4 responses (one file per entry)
├── section2_parsed/                 ← parsed fix/import/pom blocks
├── section3_baseline/               ← baseline OD reproduction results
├── section4_compilation/            ← apply + compile results (PASS/FAIL/NA)
├── section5_test_runs/              ← OD test run results (PASSED/FAILED/etc.)
├── section6_categories/             ← patch categorization
└── section7_results/                ← final assembled CSV/summary
```

---

## Sections

### Section 1 — Patch Generation (`section1_generate_patches.py`)
- Reads `flaky_test_data_no_suspect.json`
- Builds a single user-role prompt per entry using `PROMPT_TEMPLATE`
- Calls `gpt-4`, `temperature=0.2`, single user message (no system role)
- Returns raw response text
- **Key function**: `prompt_gpt4(entry: dict) -> str`
- API key: reads `OPENAI_API_KEY` env var; falls back to `input()` prompt if missing
- Output dir: `section1_patches/`

### Sections 2–7 — Not yet implemented
Intended to mirror `../patch_evaluation/section2_parse_patches.py` through
`../patch_evaluation/section7_assemble_csv.py` with any OpenAI-specific adaptations.

---

## Prompt Format (Section 1)
The full prompt is one user message combining system context + task instructions.
Structure:
1. Role preamble (testing expert)
2. Test metadata (type, victim, polluter)
3. Flakiness description
4. Reproduction steps
5. Error info (messages + failing lines)
6. Code context (global vars, helper methods, full test code)
7. Output format instructions:
   - Fix code between `//<fix start>` ... `//<fix end>`
   - pom.xml deps between `<!-- <pom.xml start> -->` ... `<!-- <pom.xml end> -->`
   - Imports between `//<import start>` ... `//<import end>`

---

## Key Decisions / Notes
- `suspect_lines` deliberately excluded from prompts (ablated variant)
- Source dataset (`../flaky_test_data.json`) had a duplicate entry for
  `ElectionListenerManagerTest.assertLeaderElectionWhenRemoveLeaderInstancePathWithAvailableServerButJobInstanceIsShutdown` — removed, now confirmed 50 unique entries
- Repos live in `../repos/`; baseline reproduction logic should reuse `../patch_evaluation/section3_baseline.py` patterns
