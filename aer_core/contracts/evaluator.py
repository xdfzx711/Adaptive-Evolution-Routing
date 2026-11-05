from typing import Protocol, runtime_checkable

from aer_core.schemas.execution import EvaluationResult, ExecutionResult
from aer_core.schemas.runtime import RunContext
from aer_core.schemas.task import EvaluationSpec, Task
from aer_core.schemas.scoring import ScoringRules


@runtime_checkable
class ScoringRulesProvider(Protocol):
    """Optional public description of the actual scorer; never includes private labels."""

    def public_scoring_rules(self) -> ScoringRules: ...


@runtime_checkable
class EvaluatorPlugin(Protocol):
    """Grade a matching task/execution/spec and return their explicit identities.

    Raise EvaluationError for scorer failure; do not fabricate a score of 0.
    EvaluationSpec and private evidence must not become public feedback.
    """

    scorer_version: str
    score_policy: str

    def evaluate(
        self, task: Task, result: ExecutionResult, spec: EvaluationSpec,
        run_ctx: RunContext,
    ) -> EvaluationResult: ...
