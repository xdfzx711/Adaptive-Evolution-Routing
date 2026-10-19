from typing import Protocol, runtime_checkable

from aer_core.schemas.execution import EvaluationResult, ExecutionResult
from aer_core.schemas.runtime import RunContext
from aer_core.schemas.task import EvaluationSpec, Task


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
