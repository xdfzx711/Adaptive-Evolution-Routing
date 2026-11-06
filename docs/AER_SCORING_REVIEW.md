# Scoring extension review

Scope: the seven-step scoring-interface change, including the IFBench native
evaluator. No existing HotpotQA baseline YAML, acceptance formula, outer-round
limit, router, or worker scheduling code was changed. IFBench dataset selection
and an end-to-end model baseline are outside this change.

## Reviewed behavior

- Missing `assessment.scoring` returns the original evaluator instance. HotpotQA
  retains its previous scorer fingerprint and numerical results. New fields in
  evaluation/trial records are optional, so historical records remain readable.
- Explicit scoring is applied at both plugin assembly boundaries: outer
  `prepare_benchmark` and GEPA's isolated candidate trials. Native evaluator
  settings must match; trial overrides cannot silently switch objectives.
- Native success is preserved even when a different optimization metric is
  selected. Raw metrics remain available. Projection failures are evaluation
  errors, and do not turn into score zero or lose known evaluator usage.
- Metric/transform changes enter scorer identity and cache conditions. Acceptance
  thresholds are recorded separately and do not alter single-execution cache keys.
- The protocol distinguishes per-execution scoring, GEPA sample means, group
  optimization means and official dataset aggregation. Binary lattice reporting
  is explanatory and never changes thresholds.
- IFBench uses the unmodified pinned official evaluation wrapper, strict before
  loose, on copied kwargs. The original response's whitespace is preserved.
- IFBench and NLTK imports are lazy. Missing resources fail before the upstream
  package can initiate downloads. Dependency setup is an explicit command.
- IFBench scorer identity includes actual checker code, wrapper code, relevant
  dependency versions and NLTK resource contents. Vendored source/license files
  are pinned to LF to preserve provenance on Windows checkouts.

## Issues found and fixed during review

1. Python 3.12+ runtime Protocol checks do not discover capabilities delegated
   only through `__getattr__`. The configured evaluator now explicitly exposes
   metric declarations and the success policy, keeping official aggregation active.
2. Scoring protocol construction originally attempted validation groups for
   evolution-disabled runs. Such runs now omit evolution-group lattice reporting.
3. Internal optimization receipts previously kept only public scalar feedback.
   They now retain the selected raw metric/conversion details without exposing
   private scoring parameters or forwarding all metrics to the diagnosis model.

## Validation

The focused regression command passed 65 tests:

```powershell
.venv/Scripts/python.exe -m unittest tests.benchmarks.test_hotpotqa tests.benchmarks.test_scoring_contract tests.benchmarks.test_ifbench_evaluator tests.validation.test_weighted_acceptance tests.integration.test_hotpotqa_baseline tests.integration.test_parallel_batch tests.integration.test_benchmark_startup -q
```

Coverage includes fixed-range direction conversion, invalid/missing metrics,
legacy HotpotQA behavior, native success separation, scorer/cache identity,
real GEPA optimizer code with deterministic model fixtures, and three spawned
workers carrying the same explicit scoring profile.

The IFBench parity test checks all 300 bundled official prompts with a fixed
synthetic response against direct official strict/loose evaluation. This is
scorer correctness evidence, not model accuracy or a completed IFBench baseline.
No paid model calls were made by these tests.

The repository-wide run (`python -m unittest discover -s tests -q`) completed
772 tests in 1132.681 seconds: `OK (skipped=4)`, with no failures or errors.
The final focused 65-test run includes the completed IFBench resource setup and
the final scoring/parallel refinements made while the full suite was running.
The full log is available locally at `.aer-cache/scoring-review-tests.log`.
Three skips are existing opt-in native Codex/MXC tests. IFBench's optional test
class was skipped before NLTK preparation finished in the first full run; all its
tests subsequently ran successfully in the focused regression run.

Packaging also passed with `pip wheel . --no-deps --no-build-isolation`. The
built wheel was inspected for the new scoring modules, IFBench wrapper, license,
provenance file and explicit NLTK preparation command. `git diff --check` passed.
