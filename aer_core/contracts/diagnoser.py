from typing import Protocol, runtime_checkable

from aer_core.schemas.execution import Diagnosis, ExecutionView, FeedbackView
from aer_core.schemas.runtime import RunContext
from aer_core.schemas.task import TaskView


@runtime_checkable
class DiagnoserPlugin(Protocol):
    """Use public views only. Return actual usage with model diagnoses;
    failures retain known usage in DiagnosisError. No target selection here.
    """

    def diagnose(
        self, task: TaskView, result: ExecutionView, feedback: FeedbackView,
        run_ctx: RunContext,
    ) -> Diagnosis: ...
