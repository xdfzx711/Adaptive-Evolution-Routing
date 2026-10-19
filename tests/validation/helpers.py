from pathlib import Path

from aer_core.config.models import ValidationSettings
from aer_core.schemas.evolution import EvolutionCandidate
from aer_core.schemas.execution import EvaluationResult, ExecutionResult
from aer_core.schemas.paired import ScoredExecution
from aer_core.schemas.references import AgentSnapshot
from aer_core.schemas.usage import ResourceUsage
from aer_core.schemas.validation import PairSeed, ValidationPlan
from plugins.storage.artifacts import LocalArtifactStore
from plugins.storage.versions import LocalVersionStore
from tests.domain_helpers import task


def settings(**updates):
    values = dict(plugin="paired_validation", split="validation", group_field="validation_group",
        similar_group="similar", regression_group="regression", acceptance_policy="improvement_with_regression_guard",
        policy_version="v0.1", min_improvement=0.01, max_regression_drop=0.0)
    values.update(updates)
    return ValidationSettings(**values)


class ScoreExecutor:
    def __init__(self, make_score=None, unknown=False):
        self.calls = []
        self.unknown = unknown
        self.make_score = make_score or (lambda task, snapshot: float(task.task_id.startswith("reg") or snapshot.parent_version_id is not None))

    def execute(self, task, snapshot, ctx):
        self.calls.append((task, snapshot, ctx))
        score = self.make_score(task, snapshot)
        unknown = self.unknown and snapshot.parent_version_id is not None
        usage = ResourceUsage(input_tokens=None if unknown else 1, output_tokens=None if unknown else 1,
            total_tokens=None if unknown else 2, model_calls=1, tool_calls=0, elapsed_seconds=0.01,
            usage_complete=not unknown, usage_sources=["unknown" if unknown else "simulated"])
        execution = ExecutionResult(task_id=task.task_id, agent_version_id=snapshot.version_id,
            operation_id=ctx.operation_id, output={"answer": score}, trajectory=[], state_summary={}, usage=usage, status="completed")
        evaluation = EvaluationResult(evaluation_id=ctx.operation_id + "/eval", task_id=task.task_id,
            agent_version_id=snapshot.version_id, execution_operation_id=ctx.operation_id, operation_id=ctx.operation_id + "/eval",
            score=score, success=score == 1, metrics={}, feedback="private grading detail")
        return ScoredExecution(task=task.identity, version_id=snapshot.version_id, seed=ctx.seed,
            execution=execution, evaluation=evaluation, usage=usage, error=None)


def fixture(root, kind="prompt", candidate_id="c1"):
    artifacts = LocalArtifactStore(root)
    versions = LocalVersionStore(root, artifacts)
    ref = artifacts.put(b"old prompt", "prompt")
    stable = AgentSnapshot(version_id="v1", agent_plugin_id="fixture_agent", config={"max_steps": 2, "nested": [1]}, artifacts={"prompt": ref})
    versions.initialize(stable)
    update = artifacts.put(b"new reusable component", kind)
    candidate = EvolutionCandidate(candidate_id=candidate_id, parent_version_id=stable.version_id, target=kind,
        artifact_updates={kind: update})
    snapshot = versions.materialize(stable, candidate)
    candidate = candidate.replace(candidate_version_id=snapshot.version_id)
    similar, regression = [task("similar1")], [task("reg1"), task("reg2")]
    plan = ValidationPlan(current_task=task("local"), baseline_execution=None, baseline_evaluation=None,
        similar_tasks=similar, regression_tasks=regression,
        pair_seeds=[PairSeed(task=item.identity, seed=index + 10) for index, item in enumerate(similar + regression)],
        policy_id="improvement_with_regression_guard", policy_version="v0.1")
    return artifacts, versions, stable, candidate, plan
