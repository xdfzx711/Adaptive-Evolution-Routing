# IFBench baseline implementation review — 2026-11-05

## Scope

Added an IFBench benchmark plugin, deterministic exclusive selection, preparation
command, raw text collector, 80-task fixed GEPA configuration and matching data
manifest/report. The pre-existing scoring work is reused. HotpotQA configuration,
scoring, JSON collector, acceptance formula and thresholds were not changed by
this integration. Prediction export typing now permits a JSON array as well as
the existing object format; existing exporters keep their output shape.

## Reviewed properties

- 80 current + 80 similar + 80 regression tasks, all IDs and normalized prompts
  globally unique. 68/12 single/two-constraint current tasks; 60 rows unused.
- Exact-signature similar tasks and same-count/disjoint-family regression tasks.
  No model or score-based selection, no silent relaxation when pairing fails.
- A pinned source hash, manifest assignment evidence and episode order are
  checked before execution. Review added order-sensitive validation, as ordinary
  dictionary equality would otherwise permit manually reordered episodes.
- Custom local inputs are identified as `provided_local`, not official test
  data. Their provenance report does not falsely attribute the pinned source.
- Public task input contains the original prompt only; checker kwargs remain
  private. The later check-level feedback extension explicitly discloses checker
  IDs and verdicts to diagnosis/reflection, not to task input. The raw collector preserves whitespace/code fences and
  supplies no output JSON schema. Missing non-string output raises an error.
- Per-task holdouts stay out of GEPA's internal task set; the common framework
  applies them during outer acceptance/reporting. Inner/outer scorer identities
  agree, including the explicit `prompt_level_loose` profile.
- The selector is deliberately capability-based and excludes singleton
  signatures from the current-task role. This selected official-test adaptation
  experiment is not an independent official full-test evaluation.

## Automated verification

99 targeted tests passed (52.734 s), covering IFBench selection/evaluator and
baseline integration, HotpotQA selection/evaluator/baseline, scoring contracts,
parallel batch execution, benchmark startup, plugin registry, workspace links
and weighted acceptance. Log: `.aer-cache/ifbench-baseline-review-tests.log`.

After the two review fixes above, all six new selection/collector/integration
tests passed again (6.764 s). The integration test uses real GEPA and official
IFBench scoring with an offline Codex-shaped transport and mocked reflection
provider. It verifies 0→1 score changes, candidate commit, early success stop,
independent reset, all holdout reports and identical inner/outer scorer versions.
This deterministic test is distinct from real-model performance evidence.

`git diff --check` passed. New Python source files passed syntax parsing.

## Real-model smoke runs

Both used the unchanged 80-task configuration with three workers and
`--report-holdouts`:

| Run ID | Current IDs | Result | Wall time |
| --- | --- | --- | --- |
| `ifbench-gepa-3-r3p3-smoke-20261105-162731` | 78, 118, 88 | completed | 107.9 s |
| `ifbench-gepa-next3-r3p3-smoke-20261105-162901` | 103, 207, 17 | completed | 118.4 s |

The second batch is the next three tasks in fixed manifest order. Across the six
current tasks, all passed initially and therefore stopped before evolution.
Similar tasks passed 5/6 and regression tasks 6/6. All initial/final holdout
reports completed. These results verify execution, official scoring, early stop
and reporting, but do not demonstrate a benefit from evolution.

A separate diagnostic run, `ifbench-gepa-forced-round-smoke-20261105-163159`,
uses current task 78 with `stop_on_success=false` and one outer round to exercise
GEPA despite initial success. It completed nine internal executions and two real
Qwen reflection calls. All candidate scores tied at 1; GEPA returned
`best_is_seed` and retained the original prompt. The diagnosis path returned
`no_failure` without a failure-analysis LLM call because the initial evaluation
passed. This diagnostic configuration is not the 80-task baseline and is not
combined with baseline performance statistics.

Run artifacts are under `runs/<run-id>/`; console logs are in
`.aer-cache/<run-id>.log`. The full 80-task experiment has not been launched.
# Check-level diagnosis follow-up (2026-11-05)

Reviewed the optional feedback schema, explicit disclosure allowlist, standalone/
outer/GEPA propagation, structured diagnosis correction and fallback, costs and
cache identity. Official IFBench scoring and weighted acceptance are unchanged.
Old benchmarks without public checks retain their existing diagnosis path.

The previous task 149 misdiagnosis is covered by an offline fixture: an old-style
claim that the keyword is missing cannot become the authoritative diagnosis when
the official keyword check passed. Actual per-check verdict conflicts, missing/
duplicate/invented IDs and invalid evidence references are also tested. Correction
is limited to one call; repeated invalid analysis becomes unknown-cause feedback.
Infrastructure failures still propagate as errors. Raw responses and attempts
remain auditable, while factual observations are evaluator-authored.

Review limitation: structural consistency does not verify the truth of every
natural-language causal hypothesis. These remain explicitly labelled hypotheses.
Reruns use separate run IDs and unchanged dataset assignments and score thresholds.

Follow-up validation: 139 tests passed in 88.887 s, covering new checked diagnosis,
legacy diagnosis, feedback isolation, IFBench/HotpotQA integration, parallel batches,
scoring, GEPA, schemas and weighted acceptance. 518 stored old evaluations and an
old diagnosis receipt were also deserialized successfully. Syntax and diff checks passed.

Real-model runs (each includes one similar and one regression task per current task):

- `runs/ifbench-checks-replay3-20261105-v1`: current 28/149/72; all episodes completed
  in 595.14 s. Current success 2/3 -> 3/3, similar 2/3 -> 3/3, regression 2/3 -> 2/3.
  Task 149 received a correct check-level diagnosis, then committed a candidate in
  round 2: current 0->1, similar 0->1, regression 0->0, weighted gain 0.75.
  Its 18 inner responses and 12 outer responses match official replay. The new
  candidate passed all three inner samples and the outer current-task execution.
- `runs/ifbench-checks-new3-20261105-v1`: current 91/68/117, chosen in manifest order
  from previously untested current tasks. All episodes completed in 67.06 s.
  Current 3/3 -> 3/3, similar 2/3 -> 2/3, regression 3/3 -> 3/3. All current tasks
  passed initially, so these episodes test execution/reporting, not evolution gains.
  All nine outer responses match official replay.

Each run contains `scoring_audit.json`. These small real-model samples demonstrate
a functioning improvement case, not a causal ablation or a full-benchmark estimate.
The earlier replay group's regression task 271 changed from an initial failure in
the pre-change run to an initial success here; this is rerun variability, not a
within-episode evolution gain. Provider-level deterministic seeding is unsupported.
