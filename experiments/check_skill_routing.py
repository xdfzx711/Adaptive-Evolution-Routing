"""Router-only synthetic contrast checks; labels are never sent to the model.

Run with python -m experiments.check_skill_routing CONFIG. This calls the configured
Router but never executes Codex, an evolver, validation, or a version commit.
"""

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path

from aer_core.errors import AERError
from aer_core.schemas.evolution import EvolutionContext
from aer_core.schemas.execution import ExecutionView, FeedbackView, TraceEvent
from aer_core.schemas.runtime import RunContext
from aer_core.schemas.skills import SkillLibrary
from aer_core.schemas.task import TaskView
from aer_core.schemas.usage import ResourceUsage
from aer_core.usage import sum_usage
from plugins.skills.library import encode_library, parse_skill
from .configuration import load_experiment_config
from .evolution import assemble_evolution
from .execution import prepare_execution


CASES = Path(__file__).parent / "data" / "skill_routing_cases.json"


def case_context(case, evolution):
    """Construct only public, explicitly synthetic evidence; never expose labels."""
    storage = evolution.execution.storage
    stable = storage.version_store.stable()
    skill = parse_skill("---\nname: ledger-migration\ndescription: Use for Acme Ledger v2 to v3 record migrations.\n---\n"
                        + case["skill_body"] + "\n")
    library = storage.artifact_store.put(encode_library(SkillLibrary(skills=[skill])), "skill")
    stable = stable.replace(version_id="synthetic-" + case["id"], artifacts={**stable.artifacts, "skill": library})
    operation = "synthetic-" + case["id"]
    state = {"synthetic": True, "agent_plugin": stable.agent_plugin_id, "skill_delivery": "mcp",
             "skill_library_artifact_id": library.artifact_id, "available_skills": [skill.name]}
    trace = []
    if case["discovered"] is not None:
        state.update(skill_discovered=case["discovered"], skill_setup_status="ready")
    if case["read"] is not None:
        state.update(read_skills=[], skill_reads=[])
        if case["read"]:
            receipt = {"call_id": "synthetic-read", "name": skill.name, "delivery": "read", "status": "completed",
                       "snapshot_version_id": stable.version_id, "skill_library_artifact_id": library.artifact_id,
                       "execution_operation_id": operation,
                       "content_digest": hashlib.sha256(skill.content.encode("utf-8")).hexdigest()}
            state.update(read_skills=[skill.name], skill_reads=[receipt])
            trace.append(TraceEvent(seq=1, kind="tool_result", payload={"name": "read_skill",
                "output": {**receipt, "content": skill.content}}))
    trace.append(TraceEvent(seq=2, kind="observation", payload={"synthetic_observation": case["observation"]}))
    config = evolution.execution.benchmark.loaded_config.config
    run = RunContext(run_id=storage.run_id, episode_id=operation, operation_id=operation, stage="routing",
                     seed=config.experiment.seed, budget=None, workspace_ref=str(storage.run_root),
                     model_timeout_seconds=config.limits.model_call_timeout_seconds,
                     tool_timeout_seconds=config.limits.tool_call_timeout_seconds,
                     max_output_tokens=config.limits.max_output_tokens_per_call)
    feasible = evolution.feasible_targets(stable)
    return EvolutionContext(task=TaskView(task_id=case["id"], benchmark_id="synthetic-routing-check",
            input={"instruction": case["instruction"]}, public_metadata={"synthetic": True}),
        stable_snapshot=stable,
        execution=ExecutionView(task_id=case["id"], agent_version_id=stable.version_id,
            output={}, trajectory=trace, state_summary=state, usage=ResourceUsage.zero()),
        feedback=FeedbackView(score=1.0 if case["success"] else 0.0, success=case["success"],
            reason_code="correct" if case["success"] else "incorrect_answer", public_feedback=case["feedback"]),
        diagnosis=None, history=[], feasible_targets=feasible, remaining_budget=None,
        as_of=datetime.now(timezone.utc), run_context=run.to_record(),
        target_descriptions={target: evolution.resolver.resolve(target).operator.capabilities().target_descriptions.get(target, target)
                             for target in feasible})


def check_cases(evolution, cases, *, progress=None):
    results, usage = [], []
    initial = evolution.execution.storage.version_store.stable()
    required = {"prompt", "tool", "skill", "model"}
    if not required <= set(evolution.feasible_targets(initial)):
        raise ValueError("Routing check requires feasible prompt, tool, skill and model targets")
    for case in cases:
        if progress:
            progress(case["id"])
        context = case_context(case, evolution)
        result = {"id": case["id"], "expected_targets": case["expected_targets"],
                  "review_note": case["review_note"], "target": None, "rationale": None,
                  "matched": None, "error": None, "record_ref": None}
        try:
            decision = evolution.router.select(context)
            result.update(target=decision.target, rationale=decision.rationale,
                          matched=decision.target in case["expected_targets"] if case["expected_targets"] else None,
                          record_ref=decision.record_ref.model_dump(mode="json") if decision.record_ref else None)
            usage.append(decision.usage or ResourceUsage.zero())
        except AERError as failure:
            info = failure.to_info()
            result.update(error=info.model_dump(mode="json"), matched=False if case["expected_targets"] else None)
            if info.usage is not None:
                usage.append(info.usage)
        results.append(result)
    scored = [item for item in results if item["expected_targets"]]
    return {"schema_version": "aer.skill_routing_check.v1", "synthetic": True,
            "note": "Contrast checks, not task performance or causal skill benefit. Unscored cases require rationale review.",
            "matched": sum(item["matched"] is True for item in scored), "scored": len(scored),
            "errors": sum(item["error"] is not None for item in results),
            "manual_review": sum(not item["expected_targets"] for item in results),
            "usage": sum_usage(usage).model_dump(mode="json"), "cases": results}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("config", type=Path)
    parser.add_argument("--set", dest="overrides", action="append", default=[])
    args = parser.parse_args(argv)
    loaded = load_experiment_config(args.config, overrides=args.overrides)
    if loaded.config.evolution.mode != "llm":
        parser.error("Use an adaptive (evolution.mode=llm) routing configuration")
    execution = prepare_execution(loaded)
    evolution = assemble_evolution(execution)
    report = check_cases(evolution, json.loads(CASES.read_text(encoding="utf-8")),
                         progress=lambda name: print(f"[routing] {name}", flush=True))
    path = execution.storage.run_root / "skill_routing_check.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Matched: {report['matched']}/{report['scored']}; errors: {report['errors']}; manual review: {report['manual_review']}")
    print(path)
    return 0 if not report["errors"] and report["matched"] == report["scored"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
