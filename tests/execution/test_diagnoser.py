import unittest
from dataclasses import replace

from aer_core.contracts.diagnoser import DiagnoserPlugin
from aer_core.errors import ContractError
from aer_core.schemas.execution import ExecutionView, FeedbackView, TraceEvent
from aer_core.schemas.states import RunStage
from aer_core.schemas.usage import ResourceUsage
from plugins.diagnosers.lightweight import LightweightDiagnoser
from tests.domain_helpers import task_view
from tests.execution.helpers import context


class DiagnoserTests(unittest.TestCase):
    def diagnose(self, success=False, reason="incorrect_answer", events=(), summary=None):
        view = ExecutionView(task_id="dev-001", agent_version_id="v1", output={}, trajectory=events,
                             state_summary=summary or {}, usage=ResourceUsage.zero())
        feedback = FeedbackView(score=float(success), success=success, reason_code=reason, public_feedback="public only")
        result = LightweightDiagnoser().diagnose(task_view(), view, feedback, replace(context(), stage=RunStage.DIAGNOSIS))
        self.assertTrue(set(result.evidence_step_ids).issubset({e.seq for e in events}))
        self.assertIsNone(result.confidence)
        self.assertNotIn("target", result.model_dump())
        return result

    def event(self, seq, kind, **payload):
        return TraceEvent(seq=seq, kind=kind, payload=payload)

    def test_success_has_priority_over_recovered_tool_error_and_other_events(self):
        self.assertIsInstance(LightweightDiagnoser(), DiagnoserPlugin)
        events = [self.event(3, "tool_result", status="error"), self.event(7, "finish")]
        result = self.diagnose(success=True, reason="correct", events=events)
        self.assertEqual(result.reason_code, "no_failure")
        self.assertEqual(result.evidence_step_ids, (7,))

    def test_step_limit_has_priority_over_missing_output(self):
        result = self.diagnose(reason="invalid_output_format", events=[self.event(9, "step_limit", max_steps=1)])
        self.assertEqual(result.reason_code, "step_limit")
        self.assertEqual(result.evidence_step_ids, (9,))

    def test_format_error_references_parse_event_or_last_response(self):
        for events, expected in [([self.event(4, "output_parse_error")], (4,)),
                                 ([self.event(2, "model_response"), self.event(8, "model_response")], (8,))]:
            result = self.diagnose(reason="invalid_output_format", events=events)
            self.assertEqual(result.reason_code, "invalid_output_format")
            self.assertEqual(result.evidence_step_ids, expected)

    def test_wrong_answer_with_tool_error_does_not_assert_causality(self):
        result = self.diagnose(events=[self.event(2, "tool_result", status="error"), self.event(8, "model_response")])
        self.assertEqual(result.reason_code, "incorrect_answer")
        self.assertIn("unknown", result.explanation)
        self.assertEqual(result.evidence_step_ids, (2, 8))

    def test_explicit_tool_termination_and_unknown_failure(self):
        result = self.diagnose(reason="other", events=[self.event(5, "tool_result", status="error")],
                               summary={"termination_reason": "tool_error"})
        self.assertEqual(result.reason_code, "tool_error")
        self.assertEqual(self.diagnose(reason="other").reason_code, "unknown_failure")

    def test_diagnosis_is_repeatable_and_requires_matching_task(self):
        events = [self.event(5, "model_response")]
        self.assertEqual(self.diagnose(events=events), self.diagnose(events=events))
        view = ExecutionView(task_id="foreign", agent_version_id="v1", output={}, trajectory=[], state_summary={}, usage=ResourceUsage.zero())
        feedback = FeedbackView(score=0, success=False, reason_code="incorrect_answer", public_feedback="public")
        with self.assertRaises(ContractError):
            LightweightDiagnoser().diagnose(task_view(), view, feedback, context())
