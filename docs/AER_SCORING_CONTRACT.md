# Benchmark scoring and weighted acceptance

This is an optional extension of the existing evaluator interface. Old YAML files
without `assessment.scoring` retain their original evaluator, score, success rule,
scorer version and evaluation cache conditions. The weighted acceptance policy
remains `weighted_improvement/v0.1`; its formula and thresholds are unchanged.

## Responsibilities

1. The native evaluator runs the official checks and preserves their values in
   `EvaluationResult.metrics`. It determines `success` independently of the
   optimization score. Evaluator failures remain errors, not zero scores.
2. An optional `MetricDefinitionProvider` declares metric name, direction,
   official bounds, binary/discrete/continuous kind and dataset aggregation.
   Unknown properties remain unknown. These are adapter-maintainer declarations
   based on official definitions, not metadata we assume every benchmark ships.
3. `assessment.scoring` selects a metric and a fixed transformation. The common
   `ConfiguredScoringEvaluator` projects `metrics[metric]` into the existing
   0..1 `score`, without changing native metrics or success.
4. GEPA and AER use the same configured single-execution evaluator. AER applies
   its configured weights, minimum gain and regression guard to optimization
   scores. LLM diagnosis does not supply or modify scores.

## Configuration

HotpotQA, explicit equivalent of the existing Joint F1 objective:

```yaml
assessment:
  evaluator: hotpotqa_evaluator
  evaluator_config:
    primary_metric: joint_f1
  diagnoser: llm_diagnoser
  scoring:
    metric: joint_f1
    transform:
      type: identity
```

The optional `scoring.metric` is the final optimization objective, overriding
the native evaluator's default score selection (`primary_metric` for HotpotQA).
Native `metrics` and native `success` are always retained. An explicit profile
gets a distinct scorer version even when it is numerically equivalent to the
legacy selection. Existing baseline YAML files have deliberately not been edited.

IFBench evaluator configuration (the benchmark adapter must supply an
`ifbench.official.v1` EvaluationSpec and an unmodified response string):

```yaml
assessment:
  evaluator: ifbench_evaluator
  evaluator_config:
    nltk_data_path: ../../.aer-cache/ifbench/nltk_data
  diagnoser: llm_diagnoser
  scoring:
    metric: prompt_level_loose
    transform:
      type: identity
```

`identity` requires a declared [0, 1] higher-is-better metric. `fixed_range`
uses `(x - minimum)/(maximum - minimum)` and reverses it for lower-is-better
metrics. Bounds come from explicit configuration, or from the official metric
declaration when configuration omits both bounds:

```yaml
scoring:
  metric: points
  transform:
    type: fixed_range
    minimum: 0
    maximum: 100
```

No per-batch min/max fitting, automatic clipping, inferred direction or LLM
reward is used. Missing metrics, invalid bounds, unsupported direction and
out-of-range observations fail explicitly. An unbounded metric requires an
explicit, appropriate conversion policy before it can be an optimization score;
this release does not provide arbitrary nonlinear conversion plugins.

## Aggregation and thresholds

Optimization uses arithmetic means of per-execution scores. GEPA's existing
`samples_per_evaluation` controls repeated trials (default 3); three binary
trials can yield an internal mean of 0, 1/3, 2/3 or 1. Outer validation continues
to use one execution per task/version/seed and arithmetic means within groups.
This does not make the outer score a three-trial success estimate.

For binary scoring with one current, one similar and one regression task and
weights 0.5/0.25/0.25, weighted changes lie on a 0.25 lattice. A configured gain
threshold of 0.04 therefore requires at least a positive 0.25 lattice change.
A regression guard of 0.10 allows no net loss of the single regression success.
The protocol reports these consequences without changing the configuration.
Lattice bounds do not guarantee an attainable improvement from a particular
baseline state. No lattice is inferred for general discrete/continuous metrics.

Dataset-level official metrics can aggregate differently. IFBench instruction
accuracy is total passed constraints / total constraints, not the unweighted
mean of each task's constraint fraction. `official_native_metrics_evaluated`
uses declared aggregation; the existing `mean_native_metrics_evaluated` remains
an explicitly named per-task arithmetic mean for compatibility. Holdout reports
also include declared official metrics for initial and final observations.

## Records and caching

With explicit scoring, `baseline_protocol.json` and `baseline_summary.json`
include the resolved metric declaration, conversion, success rule, scorer
identity, inner/outer aggregation and threshold interpretation. Each evaluation
has optional `score_details` containing the selected raw metric, conversion and
scoring version. Round reports keep these details and the original metrics.

The scorer identity and validation cache conditions include the resolved scoring
profile. Changing a metric, bounds, direction or scorer invalidates old scoring
evidence. Changing only acceptance thresholds does not change a single score.
Old evaluation records lacking `score_details` still deserialize normally.

## Public check feedback and grounded diagnosis

Evaluators may attach optional `public_checks` with `check_id`, a
`passed`/`failed`/`unknown` status, and public requirement/explanation text.
The benchmark must explicitly allow each ID in `TaskFeedbackPolicy.public_check_ids`;
the default discloses none. The same filtering applies to standalone execution,
outer evolution contexts and isolated GEPA trials. Raw evaluator prose, private
parameters and expected answers are not automatically forwarded.

IFBench discloses the per-instruction official loose verdicts already recorded
in its metrics. It does not disclose checker kwargs or reference answers.
The native scoring formula, optimization metric, success criterion and acceptance
thresholds are unchanged. The IFBench scorer fingerprint changes for this new
evidence format so old cached evaluations without checks are not reused silently.
HotpotQA's native scorer identity is unchanged.

When checks are present, the LLM diagnoses failed checks using structured hypotheses.
The framework constructs factual observations and the top-level verdict directly
from official results. Model output must cover each supplied ID exactly once,
preserve its status, reference valid trajectory events, and give no failure
hypotheses for passed/unknown checks. A conflicting or malformed response receives
one correction attempt. If it remains invalid, the framework retains the official
facts with unknown causes (`consistency_status=fallback`). Provider failures remain
errors; they are not converted into causal fallbacks or zero evaluation scores.
Both attempts and their actual costs are recorded. `verified`/`corrected` indicate
structured consistency, not semantic proof that a free-text causal hypothesis is true.

Diagnosis reuse includes public task input, execution version/output/trajectory,
assessment version and check results in its evidence fingerprint. Identical
evidence can reuse a diagnosis; changed evidence invalidates it. Benchmarks without
checks use the evidence-reference diagnosis flow described in
[AER_DOMAIN_CONTRACTS.md](AER_DOMAIN_CONTRACTS.md). GEPA reflections receive current trial
checks and distinguish them from the earlier stable-version diagnosis.

## Public rules and projected scores

Metric declarations (`MetricDefinitionProvider`) and public scorer explanations
(`ScoringRulesProvider`) are independent optional capabilities. Feedback can carry
native metrics, deterministic interpretations, and explicitly disclosed per-check
results together. Evaluation remains authoritative; a diagnostic hypothesis does
not change a score or success flag.

When explicit score projection is enabled, `ConfiguredScoringEvaluator` publishes
rules with its active projected scorer version and selected primary metric. It
preserves the native metric descriptions, interpretations and success rule, and
adds the optimization-score formula. This applies equally to outer evaluation and
GEPA trials. A native scorer without detailed public rules remains `summary_only`;
the wrapper describes the projection without inventing benchmark semantics.

The diagnosis evidence key includes the diagnosis prompt/version as well as the
public evidence. Check-level hypotheses also validate public JSON-pointer
references; historical diagnoses remove those references to avoid rebinding them
to the current task. A check-diagnosis fallback retains official check observations
in the public evolution context.

## IFBench dependency setup

The optional dependency is pinned to upstream commit
`cd189a9c2889526d8af901d2103fd3e1ffcea6df`. The official root-level
`evaluation_lib.py` is vendored unmodified with its license and provenance,
because it is not shipped in the upstream wheel. Checkers come from the pinned
optional package. Ordinary AER startup does not import IFBench or NLTK.

```powershell
.venv/Scripts/python.exe -m pip install -e ".[ifbench]"
.venv/Scripts/python.exe -m experiments.prepare_ifbench_resources
```

The second command explicitly prepares pinned official NLTK resources. Evaluation
checks for local resources before importing upstream code, preventing its
implicit download attempts during worker startup. A custom resource directory
can be passed via `--directory` and `evaluator_config.nltk_data_path`; relative
evaluator paths resolve against the experiment configuration directory.

Native IFBench specs contain `prompt`, `instruction_id_list`, aligned `kwargs`
and an optional `key` in private parameters. Output is `{"response": raw_text}`;
no response normalization precedes official checks. Strict runs before loose
on a fresh input copy, matching upstream's removal of None kwargs. Official
success is all constraints passing loose checks. Strict, loose, constraint
counts and individual constraint flags are preserved as numeric metrics.

The IFBench execution adapter, raw text collector and exclusive 80-task split
are documented separately in [IFBENCH_BASELINE.md](IFBENCH_BASELINE.md).
They reuse this scoring interface without changing the HotpotQA baseline.
