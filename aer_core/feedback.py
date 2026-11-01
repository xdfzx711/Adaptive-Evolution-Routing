"""Shared public-feedback construction; private scorer prose is never forwarded."""

from aer_core.errors import ContractError, DiagnosisError
from aer_core.schemas.execution import Diagnosis, ExecutionView, FeedbackView, TraceEvent
from aer_core.schemas.feedback import ContextPolicy
from aer_core.schemas.task import TaskIdentity, TaskView
from aer_core.schemas.validation import evaluations_match


PUBLIC_REASONS = {
    "correct": "Output format and answer are correct.",
    "no_failure": "External evaluation passed.",
    "invalid_output_format": "Final output did not meet the public format requirements.",
    "incorrect_answer": "Output format was valid, but the answer was incorrect.",
    "step_limit": "Execution reached the configured model interaction step limit.",
    "tool_error": "A tool error was observed in execution.",
    "unknown_failure": "Public evidence is insufficient to explain the failure.",
}


class PublicFeedbackBuilder:
    def __init__(self, policy: ContextPolicy):
        self.policy = policy
        self.messages = {**PUBLIC_REASONS, **dict(policy.reason_messages)}

    def feedback(self, evaluation):
        code = "correct" if evaluation.success else evaluation.reason_code
        code = code if code in self.messages else "unknown_failure"
        return FeedbackView(score=evaluation.score, success=evaluation.success,
                            reason_code=code, public_feedback=self.messages[code], assessment_version=evaluation.assessment_version)

    def diagnosis_view(self, diagnosis: Diagnosis | None, visible_steps) -> Diagnosis | None:
        if diagnosis is None:
            return None
        code = diagnosis.reason_code if diagnosis.reason_code in self.messages else "unknown_failure"
        steps = set(visible_steps)
        model = diagnosis.source == "model"
        return Diagnosis(reason_code=code, explanation=diagnosis.explanation if model else self.messages[code],
            confidence=diagnosis.confidence, source=diagnosis.source,
            evidence_step_ids=[step for step in diagnosis.evidence_step_ids if step in steps],
            observations=[item.replace(evidence_step_ids=[step for step in item.evidence_step_ids if step in steps])
                          for item in diagnosis.observations] if model else [],
            hypotheses=[item.replace(evidence_step_ids=[step for step in item.evidence_step_ids if step in steps])
                        for item in diagnosis.hypotheses] if model else [],
            fact_check=diagnosis.model_dump(mode="json")["fact_check"] if model else None, evidence_key=diagnosis.evidence_key)

    def public_views(self, task, execution, evaluation):
        """Shared filtering for standalone diagnosis and evolution contexts."""
        if task.task_id != execution.task_id or not evaluations_match(execution, evaluation):
            raise ContractError("Public feedback requires matching task, execution and evaluation", stage="evaluation")
        if self.policy.task is not None and self.policy.task != TaskIdentity(task_id=task.task_id, benchmark_id=task.benchmark_id):
            raise ContractError("Feedback policy belongs to another task", stage="evaluation")
        public = task.model_dump(mode="json")
        public["public_metadata"] = {key: value for key, value in public["public_metadata"].items() if key in self.policy.metadata_fields}
        task_view = TaskView.model_validate(public)
        result = execution.model_dump(mode="json")
        view = ExecutionView(task_id=task.task_id, agent_version_id=execution.agent_version_id,
            output={key: value for key, value in result["output"].items() if key in self.policy.output_fields},
            trajectory=self._trace(execution.trajectory), state_summary={key: value for key, value in result["state_summary"].items()
                if key in self.policy.state_fields}, usage=execution.usage)
        feedback = self.feedback(evaluation)
        return task_view, view, feedback

    def _trace(self, events):
        def usage(raw):
            if not isinstance(raw, dict):
                return {}
            result = {key: value for key, value in raw.items() if key in {
                "input_tokens", "output_tokens", "model_calls", "tool_calls", "elapsed_seconds"
            } and (value is None or type(value) in {int, float})}
            if raw.get("usage_source") in {"provider_reported", "simulated", "unknown"}:
                result["usage_source"] = raw["usage_source"]
            return result

        visible = []
        for event in events:
            raw = event.model_dump(mode="json")["payload"]
            kind = event.kind
            if kind == "model_request":
                payload = {key: raw[key] for key in ("operation_id",) if key in raw}
            elif kind == "model_response":
                payload = {key: raw[key] for key in ("operation_id", "content", "finish_reason") if key in raw}
                payload["usage"] = usage(raw.get("usage"))
                payload["tool_calls"] = [{"call_id": call.get("call_id"), "name": call.get("name"),
                    "arguments": {key: value for key, value in call.get("arguments", {}).items()
                                  if key in self.policy.tool_arguments.get(call.get("name"), ())}}
                    for call in raw.get("tool_calls", []) if isinstance(call, dict)]
            elif kind in {"tool_call", "tool_result"}:
                payload = {key: raw[key] for key in ("operation_id", "call_id", "name", "status") if key in raw}
                payload["usage"] = usage(raw.get("usage"))
                field = "arguments" if kind == "tool_call" else "output"
                rules = self.policy.tool_arguments if kind == "tool_call" else self.policy.tool_outputs
                payload[field] = {key: value for key, value in raw.get(field, {}).items() if key in rules.get(raw.get("name"), ())}
                if raw.get("error"):
                    payload["error_observed"] = True
            elif kind == "finish":
                payload = {"output": {key: value for key, value in raw.get("output", {}).items() if key in self.policy.output_fields}}
            elif kind == "step_limit":
                payload = {key: raw[key] for key in ("max_steps",) if key in raw}
            elif kind in {"output_parse_error", "execution_error"}:
                payload = {"observed": True}
            else:
                continue
            visible.append(TraceEvent(seq=event.seq, kind=kind, payload=payload, timestamp=event.timestamp))
        return tuple(visible)


def validate_diagnosis(value, execution, feedback=None):
    if not isinstance(value, Diagnosis):
        raise DiagnosisError("Diagnoser must return Diagnosis", stage="diagnosis")
    if not value.all_evidence_step_ids <= {event.seq for event in execution.trajectory}:
        raise DiagnosisError("Diagnosis references non-public evidence", stage="diagnosis",
                             usage=value.usage, evidence_ref=value.record_ref)
    if feedback is not None and not feedback.success and value.reason_code == "no_failure":
        raise DiagnosisError("A failed evaluation cannot be diagnosed as no_failure", stage="diagnosis",
                             usage=value.usage, evidence_ref=value.record_ref)
    return value
