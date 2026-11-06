# IFBench fixed GEPA baseline

The baseline contains 80 independent current tasks, each assigned one similar
task and one regression task: 240 globally distinct source keys and normalized
prompts. Each episode starts from the same initial prompt; accepted prompts do
not propagate to other episodes. The 60 unused source rows are not consumed by
this run and have not been declared an independent final test partition.

## Source and reproducible preparation

Source: [AllenAI IFBench](https://github.com/allenai/IFBench),
`data/IFBench_test.jsonl` at commit
`cd189a9c2889526d8af901d2103fd3e1ffcea6df`.
The 300-row file's SHA-256 is
`a2bf7b8e7ed11d39c65b4ec781da8b99663ed0121f97033d70fcecb61f8a9d85`.
Data license: ODC-BY-1.0; attribution is retained in the selection report.
The default preparation command copies the verified bundled package data, or
downloads the pinned file when `--download` is supplied. It does not call models.

```powershell
.venv/Scripts/python.exe -m pip install -e ".[ifbench,gepa]"
.venv/Scripts/python.exe -m experiments.prepare_ifbench_resources
.venv/Scripts/python.exe -m experiments.prepare_ifbench --download
```

The local source is `.aer-cache/ifbench/IFBench_test.jsonl` (not tracked).
Assignments are tracked in `experiments/data/ifbench_80_manifest.json`.
`experiments/data/ifbench_80_selection_report.json` contains every assigned
prompt, constraint signature, family, and pairing rationale. Runtime loading
checks the source digest and recomputes the complete deterministic selection.
Changed source contents or edited assignment evidence fail startup.

## Selection rules (seed 42)

1. Validate source structure and unique IDs; deduplicate NFKC/casefold/whitespace
   normalized prompts before assigning any role. Selection uses only `key`,
   `prompt` and `instruction_id_list`, never model responses or pass/fail scores.
2. Allocate current-task counts proportionally by constraint count using largest
   remainders: 68 single-constraint and 12 two-constraint tasks. Constraint count
   is a complexity proxy, not a claim of equal empirical difficulty.
3. Within each count, rotate across exact constraint signatures in seeded hash
   order. Each current task must have an unused exact-signature partner.
4. Similar task: identical sorted constraint IDs, including their count. Among
   available matches, choose greatest word-set Jaccard of the normalized prompt;
   ties use seeded hash order. Constraint parameters and subject matter may differ.
   Similarity here means the same instruction-following capability, not guaranteed
   topic similarity; there is no semantic-similarity cutoff.
5. Regression task: the same constraint count, with **no overlapping constraint
   family** (prefix before `:`). Exclusive bipartite matching prevents reuse. This
   checks different capabilities; regression tasks are not preselected to pass.
6. If matching fails, try at most 256 documented deterministic hash orderings,
   retaining all rules. The committed 80-task selection succeeds at ordering 0.
   Insufficient data fails preparation rather than weakening the criteria.

The current pool covers 47 distinct constraint types and six families. Family
memberships (multi-family tasks count in multiple categories): format 25, words
22, count 19, ratio 12, sentence 9, repeat 3. Singleton signatures, including the
custom constraints, cannot serve as current tasks under the exact-match rule;
they may serve as regression tasks. Thus this is a selected adaptation subset,
not an unbiased estimate of performance on all official IFBench tasks.

The official source is named `test`. AER deliberately assigns selected rows to
its runtime `dev`/`validation` roles for this adaptation experiment while keeping
`source_split=official_test`. Similar/regression tasks participate in acceptance;
their reported scores are not independent final-test scores.

## Execution and scoring

- Benchmark plugin: `ifbench_adapter`. Only the original prompt is placed in
  task input. Checker IDs/kwargs stay in the private evaluation specification.
- Collector: `raw_text_result_collector`. No JSON schema, trimming, code-fence
  removal, or answer reformatting; the final answer string becomes `response`.
  The native evaluator performs its own official strict/loose checks.
- Objective: official `prompt_level_loose`, identity projection, 0 or 1. Success
  requires all loose checks to pass. Strict, instruction-level and individual
  checker metrics remain logged. IFBench checks constraint compliance, not a
  separate factual correctness score for the underlying request.
- GEPA uses the same evaluator as the outer loop, three samples per candidate
  and task, at most nine internal executions and two reflections per round.
- Fixed prompt route, at most three outer rounds, early stop on stable success.
  Weighted gain is `0.5*current + 0.25*similar + 0.25*regression` (score deltas),
  requiring >=0.04 and regression delta >=-0.10. With these binary single-task
  groups, the positive gain step is 0.25 and a regression success cannot be lost.
- Models match the existing baseline: Codex `gpt-6.1-sol` executes tasks, root
  `.env` Qwen settings supply diagnosis/reflection. Scoring never uses an LLM.

## Run commands

Small smoke run (three current tasks plus their six distinct holdouts):

```powershell
.venv/Scripts/python.exe -m experiments run-benchmark experiments/configs/v0_1_codex_ifbench_gepa_80_r3.yaml --run-id ifbench-fixed-gepa-smoke-v1 --limit 3 --workers 3 --report-holdouts
```

Full 80-task baseline:

```powershell
.venv/Scripts/python.exe -m experiments run-benchmark experiments/configs/v0_1_codex_ifbench_gepa_80_r3.yaml --run-id ifbench-fixed-gepa-80-r3p3-v1 --workers 3 --report-holdouts
```

Use a fresh run ID for each run. `runs/<run-id>/baseline_summary.json` includes
initial/final current and holdout scores, acceptance records, errors, and cost.
`baseline_protocol.json` records scoring and acceptance rules. `predictions.json`
is a JSON array of `{key, prompt, response}` records; the upstream CLI expects
JSONL, so serialize one array entry per line before passing it to that CLI.
Task subdirectories retain detailed round, diagnosis and GEPA records.

Execution/network errors are not scored as zero. When the initial answer already
passes, stopping at round zero is expected. A diagnostic run with
`--set evolution.stop_on_success=false --set limits.max_evolution_rounds=1`
can exercise evolution even on such a task; keep it separate from baseline results.
