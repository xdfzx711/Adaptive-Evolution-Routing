import json
import unittest

from aer_core.construction_feedback import limit_construction_feedback
from aer_core.context import ContextBuilder
from aer_core.errors import EvolutionError
from aer_core.schemas.construction import ConstructionFeedback, ConstructionIssue
from aer_core.schemas.evolution import EvolutionDecision
from plugins.evolution.oscopilot.feedback import construction_feedback
from tests.domain_helpers import NOW
from tests.evolution.helpers import MemoryExperience, fixture
from tests.evolution.test_context import terminal
from tests.storage.helpers import attempt


def execution(error=None, **extra):
    return {"kind": "execution", "tool": "inspect", "repair": 1,
        "specification": {"example_arguments": {"path": "file.py"}}, "source": "def inspect(): pass",
        "result": {"status": "error" if error else "completed", "error": error, "result": {"ok": not error}, "stdout": "trace"}, **extra}


def delivery(tool="inspect", passed=False, **extra):
    return {"kind": "delivery_check", "tool": tool, "passed": passed, **extra}


def feedback(events, **kwargs):
    return construction_feedback(events, kwargs.get("calls", []), kwargs.get("status", "empty"),
        kwargs.get("error"), None, 8)


class ConstructionFeedbackTests(unittest.TestCase):
    def test_metadata_format_and_incompatible_callable_keep_distinct_history_codes(self):
        events = [{"kind": "export_failed", "tool": "builder", "error_code": "incompatible_callable",
                   "error": "Builder returns a function object", "source": "def builder(): pass", "invoke": "builder()"},
                  {"kind": "export_failed", "tool": "analyzer", "error_code": "metadata_json_invalid",
                   "error": "Missing delimiter after format recovery", "source": "def analyze(): pass", "invoke": "analyze()"}]
        issues = feedback(events).issues
        self.assertEqual([i.code for i in issues[:2]], ["incompatible_callable", "metadata_json_invalid"])
        self.assertEqual(issues[0].details["invoke"], "builder()")
        self.assertEqual(issues[1].source, "observation")

    def test_runtime_and_schema_errors_are_resolved_only_by_later_matching_evidence(self):
        events = [{"kind": "contract_error", "tool": "inspect", "repair": 0, "error": "bad schema"},
            execution("FileNotFoundError"),
            {"kind": "judgment", "tool": "inspect", "repair": 1, "status": "Complete", "score": 9, "reasoning": "Looks good"}]
        issues = feedback(events).issues
        self.assertEqual(issues[0].status, "resolved")
        self.assertEqual(issues[0].resolution_event_index, 1)
        self.assertEqual(issues[1].status, "unresolved")  # Complete never erases an actual error.
        events.extend([execution(interface_error="Wrong output type"), execution()])
        issues = feedback(events).issues
        self.assertEqual(issues[1].resolution_event_index, 3)
        self.assertEqual(issues[1].resolution_details["source"], "def inspect(): pass")
        self.assertEqual(issues[2].resolution_event_index, 4)
        self.assertEqual(issues[1].details["arguments"]["path"], "file.py")

    def test_model_critique_is_not_promoted_to_fact_and_clean_delivery_failure_remains(self):
        events = [{"kind": "judgment", "tool": "inspect", "repair": 0, "status": "Amend", "score": 4,
                   "reasoning": "Speculative singleton reduce error"}, execution(),
            {"kind": "judgment", "tool": "inspect", "repair": 1, "status": "Complete", "score": 9, "reasoning": "Repaired"},
            delivery(path_error="Old workspace absolute path", arguments={"path": "C:/old/file.py"})]
        issues = feedback(events).issues
        self.assertEqual(issues[0].source, "model_assessment")
        self.assertEqual(issues[0].status, "resolved")
        failure = next(i for i in issues if i.stage == "delivery")
        self.assertEqual(failure.source, "observation")
        self.assertEqual(failure.status, "unresolved")
        self.assertEqual(failure.details["arguments"]["path"], "C:/old/file.py")

    def test_partial_exports_keep_failed_tool_result_and_patch_diagnostics(self):
        events = [delivery(tool="good", passed=True), delivery(tool="bad",
            acceptance_error="Patch not applicable", result={"error": None, "result": {"success": False, "error_message": "missing file"}},
            patch_check={"returncode": 1, "stderr": "file.py: patch does not apply"}),
            {"kind": "export_verified", "tools": ["good"]}]
        value = feedback(events, status="completed")
        self.assertEqual(value.exported_tools, ("good",))
        self.assertEqual(value.issues[0].tool, "bad")
        self.assertFalse(value.issues[0].details["result"]["result"]["success"])
        self.assertIn("does not apply", value.issues[0].details["patch_check"]["stderr"])

    def test_plan_correction_and_request_retry_keep_resolution_and_exclude_raw_model_requests(self):
        calls = [{"purpose": "construction", "logical_call_id": "request1", "attempt": 1,
            "error": {"code": "operation_timeout", "message": "Timed out"},
            "request": {"messages": ["DO_NOT_RECURSIVELY_COPY_OLD_CONTEXT"]}},
            {"purpose": "construction", "logical_call_id": "request1", "response": {"content": "generated source"}}]
        events = [{"kind": "plan_contract_check", "accepted": False, "review": {"nodes": {"a": "conflict"}}},
                  {"kind": "plan_contract_check", "accepted": True}]
        value = feedback(events, calls=calls, status="completed")
        self.assertTrue(all(i.status == "resolved" for i in value.issues))
        self.assertEqual(value.issues[0].source, "model_assessment")
        self.assertEqual(value.issues[1].resolution_call_index, 1)
        self.assertNotIn("DO_NOT_RECURSIVELY", value.model_dump_json())

    def test_empty_plan_limits_and_terminal_errors_are_explicit(self):
        empty = feedback([{"kind": "plan", "value": {"nodes": {}}}])
        self.assertEqual(empty.issues[0].code, "no_exportable_tools")
        failed = feedback([{"kind": "repair_limit", "tool": "inspect"}, {"kind": "graph_blocked", "tool": "inspect"}],
            status="error", error=EvolutionError("Transport failed", stage="evolution").to_info())
        self.assertEqual([i.stage for i in failed.issues], ["construction", "construction", "termination"])
        self.assertEqual(failed.issues[-1].message, "Transport failed")

    def test_history_caps_prioritize_unresolved_evidence_and_mark_omissions(self):
        value = ConstructionFeedback(operator_id="oscopilot_tool_evolver", outcome="empty", issues=[
            ConstructionIssue(stage="construction", code="repaired", source="observation", message="Recovered", status="resolved",
                details={"old_source": "x" * 10000}),
            ConstructionIssue(stage="delivery", code="path_error", source="observation", message="FileNotFoundError: C:/old/file.py",
                details={"arguments": {"path": "C:/old/file.py"}, "stdout": '"' * 10000})])
        clipped = limit_construction_feedback(value, 1800)
        self.assertLessEqual(len(clipped.model_dump_json()), 1800)
        self.assertEqual(clipped.issues[0].code, "path_error")
        self.assertTrue(clipped.truncated)
        self.assertTrue(clipped.issues[0].truncated)
        self.assertGreater(clipped.omitted_issues, 0)
        self.assertIn("C:/old/file.py", clipped.model_dump_json())
        self.assertEqual(len(value.issues[1].details["stdout"]), 10000)

    def test_feedback_survives_empty_attempt_serialization_and_scope_filtered_history(self):
        _, stable, _, task, record, live, policy = fixture()
        value = feedback([delivery(path_error="FileNotFoundError: old workspace")])
        previous = attempt(status="empty_candidates", candidate_ids=[]).replace(
            decision=EvolutionDecision(target="tool", rationale="Build tool"), construction_feedback=value)
        saved = terminal(previous)
        saved = type(saved).model_validate_json(saved.model_dump_json())
        other = terminal(previous.replace(attempt_id="other-task", task_id="dev-002"))
        builder = ContextBuilder(MemoryExperience([saved, other]), policy.replace(artifact_kinds=["prompt", "tool"]))
        state = builder.build(task, record, stable, live, round_id=2, feasible_targets=["tool"], as_of=NOW)
        self.assertEqual(len(state.history), 1)
        self.assertEqual(state.history[0].candidate_summaries, ())
        self.assertEqual(state.history[0].construction_feedback, value)
        filtered = ContextBuilder(builder.experience, policy).build(task, record, stable, live,
            round_id=2, feasible_targets=["prompt"], as_of=NOW)
        self.assertIsNone(filtered.history[0].construction_feedback)
        self.assertNotIn("old workspace", filtered.model_dump_json())

    def test_missing_legacy_feedback_is_compatible(self):
        legacy = attempt().model_dump(mode="json")
        legacy.pop("construction_feedback")
        self.assertIsNone(type(attempt()).model_validate(legacy).construction_feedback)
