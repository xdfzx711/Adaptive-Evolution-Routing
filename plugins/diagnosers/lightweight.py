from aer_core.errors import ContractError
from aer_core.schemas.execution import Diagnosis, ExecutionView, FeedbackView
from aer_core.schemas.runtime import RunContext
from aer_core.schemas.task import TaskView


class LightweightDiagnoser:
    def diagnose(self, task: TaskView, result: ExecutionView, feedback: FeedbackView,
                 run_ctx: RunContext) -> Diagnosis:
        if task.task_id != result.task_id:
            raise ContractError("Diagnosis task and execution must match", stage=run_ctx.stage,
                                operation_id=run_ctx.operation_id, plugin_id="lightweight_diagnoser")

        def evidence(*kinds):
            return tuple(event.seq for event in result.trajectory if event.kind in kinds)

        def diagnosis(code, explanation, steps=()):
            return Diagnosis(reason_code=code, explanation=explanation, evidence_step_ids=steps, confidence=None)

        if feedback.success:
            return diagnosis("no_failure", "External evaluation passed; no task failure was observed.", evidence("finish")[-1:])
        if evidence("step_limit"):
            return diagnosis("step_limit", "Execution reached the configured model interaction step limit.", evidence("step_limit")[-1:])
        if feedback.reason_code == "invalid_output_format":
            return diagnosis("invalid_output_format", "Final output did not meet the public format requirements.",
                             evidence("output_parse_error")[-1:] or evidence("model_response")[-1:])
        if result.state_summary.get("termination_reason") == "tool_error":
            errors = tuple(event.seq for event in result.trajectory if event.kind == "tool_result" and event.payload.get("status") == "error")
            return diagnosis("tool_error", "Execution explicitly terminated because of a tool error.", errors[-1:])
        if feedback.reason_code == "incorrect_answer":
            errors = tuple(event.seq for event in result.trajectory if event.kind == "tool_result" and event.payload.get("status") == "error")
            explanation = "Output format was valid, but external evaluation marked the answer incorrect."
            if errors:
                explanation += " Tool errors were observed; their causal relationship to the incorrect answer is unknown."
            steps = tuple(sorted(set(errors + evidence("model_response")[-1:])))
            return diagnosis("incorrect_answer", explanation, steps)
        return diagnosis("unknown_failure", "Public feedback and execution evidence are insufficient to explain the failure.")
