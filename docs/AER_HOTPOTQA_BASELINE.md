# HotpotQA fixed-router baseline

For the current **300-task / 1 similar / 1 regression** experiment with globally
exclusive holdouts and full initial/final reporting, see
[AER_HOTPOTQA_300_BASELINE.md](AER_HOTPOTQA_300_BASELINE.md).
The four-task configuration documented below remains available unchanged.

This integration uses HotpotQA **distractor**: the Agent receives the question,
ten context paragraphs with original sentence IDs, and a public JSON output
schema. Gold answers and supporting facts stay in `EvaluationSpec`.

## Baseline families

| Router | Decision | Component execution |
|---|---|---|
| `fixed_router` | One configured target throughout the episode | Full operator search/construction |
| `llm_router` | LLM selects a target | Same operator interfaces |
| `jev_router` | JEV selects a target | Same operator interfaces |

This release configures **Fixed-Prompt / GEPA** only. Fixed-Tool, Fixed-Skill and
Fixed-Memory can reuse the benchmark, holdout manifest and evaluator with their
own operator/Agent capability configuration. This does not imply that every
existing operator is meaningful for every task setting.

The shipped configuration uses Codex `gpt-6.1-sol`, fixed prompt routing, no
Skill or Memory library, an empty workspace and the JSON result collector.
The task and seed prompt prohibit web/shell/file access; the existing Codex
runtime denies access to project grading files and disables network access.
Execution traces remain available to check actual tool use. Qwen is used for
diagnosis and GEPA reflection, never for grading.

## Local data and reproducibility

Install optional dataset conversion and GEPA dependencies:

```powershell
.venv/Scripts/python.exe -m pip install -e ".[gepa,hotpotqa-data]"
.venv/Scripts/python.exe -m experiments.prepare_hotpotqa --download-only
```

`--download-only` restores the sources for the checked-in manifest without
changing task selection. The preparation tool fetches the HotpotQA HF dataset
at revision `1908d6afbbead072334abe2965f91bd2709910ab`, validates the three pinned
Parquet SHA-256 hashes, and converts the data to official-shaped JSON. It tries
the HF origin and then its mirror with identical hash checks. No data is fetched
during task execution. `pyarrow` is needed only for preparation, not grading.

Downloads, converted data and `download_receipt.json` live under
`.aer-cache/hotpotqa/` (Git-ignored). The dataset license is CC BY-SA 4.0; source:
<https://hotpotqa.github.io/>. The converted sources contain 90,447 official
training rows and 7,405 official development rows. The original CMU download
endpoint was unavailable during implementation; this is a pinned HF copy.

For a different selection, produce a **new** manifest:

```powershell
.venv/Scripts/python.exe -m experiments.prepare_hotpotqa --tasks 8 --similar 2 --regression 2 --test 8 --seed 43 --selection legacy-shared --output experiments/data/hotpotqa_seed43.json
```

Original JSON downloads can instead be supplied with `--train PATH --dev PATH`.
Manifests and input files are never overwritten by the selection command.
Files use relative portable paths, checksums and an explicit selection policy.
The loader verifies source IDs, role membership and source hashes. Its parse
cache retains only serialized selected rows and gives each instance fresh data;
task/model/workspace/version state is not shared across internal trials.

## Current tasks and holdouts

`experiments/data/hotpotqa_manifest.json` selects four current tasks from official
train, alternating bridge and comparison questions. For each task it specifies
two same-type similar tasks and two representative regression tasks. These pools
are globally disjoint from current tasks. A fixed hash order and seed determine
selection, without using labels or model scores. Different episodes may share
holdout examples; this is recorded, not an independent final test.

Four official-dev examples are reserved as AER `test`. This is a local held-out
subset, **not** HotpotQA's hidden official test. The remaining original data is
available for future separately declared experiments. Official source splits
and AER experiment roles are both recorded.

Every dev task must have exactly one `validation_sets` mapping with nonempty
`similar` and `regression` lists. The existing `TaskValidationProvider` selects
the matching lists for each episode. Missing, duplicated, overlapping or
wrong-role IDs fail setup before model execution. Manifest content contributes
to `dataset_version`, and the existing validation cache conditions include this
version, the scoring version/configuration and task specifications.

In `independent`, GEPA's internal train and selection sets both contain only
the current task. Holdout trajectories and gold labels do not enter internal
search. Each new episode starts from the same seed Snapshot. The internal
selection score measures task adaptation, not held-out generalization. Outer
holdout summaries can guide later rounds, so a repeatedly queried holdout set
must not be described as an untouched final test.

## Native scoring and acceptance

The vendored official evaluator is pinned to HotpotQA commit
`3635853403a8735609ee997664e1528f4480762a`. Its only modification replaces the
`ujson` import with standard-library `json`. Provenance, the original SHA-256
and Apache-2.0 license are included in `plugins/evaluators/hotpotqa_official/`.
The adapter uses official answer normalization, yes/no handling and supporting
fact set scoring, and follows the official joint precision/recall formula.

All twelve official metrics are retained. The default optimization score is
`joint_f1` on [0,1], and AER success means `joint_em == 1`. Missing/malformed
prediction fields are treated as absent; valid fields retain their official
credit. A runtime/parsing/scoring infrastructure error is recorded as an error,
not fabricated into a task score. Typed but incorrect evidence references are
false positives, not silently removed. The generic JSON collector still treats
an entirely unparseable final response as an execution error.

Benchmarks can opt into `NativeEvaluationProvider`. For these benchmarks,
startup requires the declared evaluator and normalized equality of any GEPA
trial evaluator override. Existing benchmarks without this protocol retain
their prior scoring options. `trial_evaluator: null` inherits the outer scorer.

The shipped acceptance policy is:

```
G = 0.50 * delta_current + 0.25 * delta_similar_mean + 0.25 * delta_regression_mean
accept iff validation is complete, G >= 0.01, and delta_regression_mean >= 0
```

GEPA internal acceptance/search remains unchanged. Each Prompt/task uses three
fresh samples, averaged before returning one score to GEPA. Nine actual internal
executions, at most two internal iterations and a 900-second internal limit are
configured per outer round. There is one outer round per episode. These are
limits, not a guarantee of two useful proposals. Outer paired validation uses
its existing single execution per version/task with compatible baseline cache
reuse. It is not a three-sample outer experiment.

Global cumulative token/time limits remain unenforced by the existing AER
controller. They must not be reported as a hard resource cap. The summary
records `total_token_budget_enforced: false` and actual reported usage.

## Run and inspect

```powershell
.venv/Scripts/python.exe main.py inspect-benchmark experiments/configs/v0_1_codex_hotpotqa_gepa.yaml
.venv/Scripts/python.exe main.py run-benchmark experiments/configs/v0_1_codex_hotpotqa_gepa.yaml --limit 1 --run-id hotpotqa-smoke
.venv/Scripts/python.exe main.py run-benchmark experiments/configs/v0_1_codex_hotpotqa_gepa.yaml --run-id hotpotqa-fixed-gepa
```

Use a fresh run ID. `--set benchmark.config.manifest_path=...` selects another
manifest; paths remain relative to the experiment configuration directory.
Single-task `run-task` remains available unchanged.

`run-benchmark` is benchmark-independent and saves `baseline_summary.json`
incrementally. It records task-specific holdouts, seed/final Prompts, initial
and final native scores, internal trials/candidates, acceptance breakdowns,
version IDs, status and usage. Full trajectories and native per-holdout metrics
remain in each round's `record.json`. `predictions.json` uses the optional
benchmark-native export interface (HotpotQA's `answer`/`sp` ID maps).

Aggregates state their evaluated-task denominator and error count. They do not
silently treat infrastructure errors as zero scores or omit the error count.
Final outputs in independent mode come from independently adapted versions,
not one universal optimized Prompt. The command does not resume old batches;
interrupted batches retain incremental summaries. In continual mode it stops
after a failed episode to avoid propagating uncertain state.

## Extending to another benchmark

Implement `BenchmarkPlugin` and `EvaluatorPlugin`; reuse `TaskManifest` and
`select_holdouts` where category-based selection is appropriate. Implement
`TaskValidationProvider` for per-current-task holdouts, opt into
`NativeEvaluationProvider` for enforced native scoring, and optionally implement
`PredictionExportProvider`. Register the plugins and add a configuration. No
GEPA search, router or acceptance algorithm changes are required. Benchmarks
needing containers, browsers or simulators still need their own execution
environment adapter.
