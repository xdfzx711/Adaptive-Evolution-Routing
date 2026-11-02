import json
from dataclasses import replace
import unittest
from unittest.mock import patch

from aer_core.execution_evidence import execution_evidence
from aer_core.schemas.evolution import EvolutionDecision, EvolutionRequest
from aer_core.schemas.execution import TraceEvent
from aer_core.schemas.skills import SkillLibrary
from aer_core.schemas.usage import BudgetRemaining, UsageReport
from plugins.evolution.skillclaw.config import SkillClawConfig
from plugins.evolution.skillclaw.evolver import SkillClawSkillEvolver
from plugins.evolution.skillclaw.exporter import apply_proposal
from plugins.evolution.skillclaw.sessions import export_session
from plugins.evolution.skillclaw.source import load_upstream, UPSTREAM_REVISION
from plugins.skills.library import decode_library, encode_library, parse_skill
from plugins.skills.runtime import SkillRuntime
from tests.evolution.helpers import evolution_context
from tests.execution.helpers import RecordingModel, response
from tests.skills.test_execution import library


def fixture(*, read=True, mcp=False, aligned=True):
    state, store, live = evolution_context()
    original = SkillLibrary(skills=[*library().skills, parse_skill(
        '---\nname: untouched\ndescription: Preserve bytes.\ncustom: value\n---\n\nKEEP\n')])
    snapshot = state.stable_snapshot.replace(artifacts={**state.stable_snapshot.artifacts,
        "skill": store.put(encode_library(original), "skill")})
    runtime = SkillRuntime.from_snapshot(snapshot, store, "exec-test")
    events = list(state.execution.trajectory)
    if read:
        output = runtime.read_skill("test-skill", call_id="read-1")
        if aligned:
            events.append(TraceEvent(seq=2, kind="tool_result", payload={"call_id": "read-1", "status": "completed",
                "name": "mcp_tool_call" if mcp else "read_skill",
                "output": {"result": {"structuredContent": output}} if mcp else output}))
    state = state.replace(stable_snapshot=snapshot, feasible_targets=["skill"],
        execution=state.execution.replace(trajectory=events, state_summary=runtime.summary("mcp" if mcp else "tool")))
    request = EvolutionRequest(attempt_id="skill-attempt", round_id=1, context=state,
        decision=EvolutionDecision(target="skill", rationale="fixed target"), operator_id="skillclaw_skill_evolver",
        max_candidates=1, run_context=live)
    return original, store, request


def proposal(action="improve_skill", **values):
    return {"action": action, "rationale": "Grounded reusable guidance", "skill": {
        "name": "test-skill", "description": "Use for the public task.", "content": "IMPROVED", **values}}


def run_workflow(outputs, *, config=None, **kwargs):
    original, store, request = fixture(**kwargs)
    responses = iter(outputs)
    model = RecordingModel(lambda *_: next(responses))
    operator = SkillClawSkillEvolver(config or SkillClawConfig(), store, model)
    result = operator.evolve(request)
    receipt = json.loads(store.get(result.record_ref))
    return original, store, request, model, result, receipt


class ExecutionEvidenceTests(unittest.TestCase):
    def test_direct_and_mcp_reads_align_and_never_become_prm(self):
        for mcp in (False, True):
            with self.subTest(mcp=mcp):
                original, _, request = fixture(mcp=mcp)
                evidence = execution_evidence(request.context, original)
                self.assertEqual(evidence.skill_reads[0].event_seq, 2)
                session = export_session(evidence, SkillClawConfig())
                self.assertEqual(session["turns"][-1]["read_skills"], [{"skill_name": "test-skill"}])
                self.assertTrue(all(turn["prm_score"] is None for turn in session["turns"]))
                self.assertEqual(session["aggregate"]["rollout_count"], 1)

    def test_missing_step_is_explicit_and_unverified_names_are_not_reads(self):
        original, _, request = fixture(aligned=False)
        evidence = execution_evidence(request.context, original)
        self.assertEqual(evidence.gaps, ("skill_read_step_alignment_unknown",))
        session = export_session(evidence, SkillClawConfig(max_events=1))
        self.assertEqual(session["turns"][-1]["source_kind"], "session_evidence")
        self.assertEqual(session["aer_provenance"]["truncations"], ["events_middle"])
        ctx = request.context.replace(execution=request.context.execution.replace(
            state_summary={"read_skills": ["test-skill"]}))
        evidence = execution_evidence(ctx, original)
        self.assertEqual(evidence.skill_reads, ())
        self.assertIn("declared_reads_without_verified_receipts", evidence.gaps)

    def test_mismatched_versions_hashes_and_conflicting_receipts_are_rejected(self):
        original, _, request = fixture()
        for key, value in (("snapshot_version_id", "other"), ("skill_library_artifact_id", "other"),
                           ("content_digest", "wrong"), ("name", "missing")):
            state = request.context.execution.model_dump(mode="json")["state_summary"]
            state["skill_reads"][0][key] = value
            ctx = request.context.replace(execution=request.context.execution.replace(state_summary=state))
            with self.subTest(key=key), self.assertRaises(ValueError):
                execution_evidence(ctx, original)
        state = request.context.execution.model_dump(mode="json")["state_summary"]
        state["skill_reads"] *= 2
        ctx = request.context.replace(execution=request.context.execution.replace(state_summary=state))
        self.assertEqual(len(execution_evidence(ctx, original).skill_reads), 1)

    def test_unready_service_is_not_a_no_skill_session(self):
        original, _, request = fixture(read=False)
        for status in ("error", "not_started"):
            state = request.context.execution.model_dump(mode="json")["state_summary"]
            state["skill_setup_status"] = status
            ctx = request.context.replace(execution=request.context.execution.replace(state_summary=state))
            with self.subTest(status=status), self.assertRaisesRegex(ValueError, "not ready"):
                execution_evidence(ctx, original)

    def test_separate_tool_result_survives_native_trajectory_rendering(self):
        original, _, request = fixture(read=False)
        events = [*request.context.execution.trajectory,
            TraceEvent(seq=2, kind="tool_call", payload={"name": "calculator", "call_id": "c1", "arguments": {"expression": "1+1"}}),
            TraceEvent(seq=3, kind="tool_result", payload={"name": "calculator", "call_id": "c1", "status": "error",
                "error_observed": True, "output": {"message": "VISIBLE_FAILURE"}})]
        ctx = request.context.replace(execution=request.context.execution.replace(trajectory=events))
        session = export_session(execution_evidence(ctx, original), SkillClawConfig())
        trajectory = load_upstream().summarizer.build_session_trajectory(session)
        self.assertIn("VISIBLE_FAILURE", trajectory)
        self.assertIn("AER source event 3: tool_result", trajectory)
        self.assertEqual(trajectory.count("calculator({"), 1)
        self.assertEqual(session["turns"][-1]["tool_errors"][0]["tool_name"], "calculator")


class SkillClawExporterTests(unittest.TestCase):
    def test_complete_library_preserves_unrelated_documents_and_frontmatter(self):
        original, _, _ = fixture()
        updated, change = apply_proposal(original, proposal(), source_name="test-skill")
        self.assertEqual(change["action"], "improve_skill")
        self.assertEqual(updated.skills[1], original.skills[1])
        self.assertEqual(decode_library(encode_library(updated)), updated)
        description, _ = apply_proposal(original, proposal("optimize_description", content="VERSION_A"), source_name="test-skill")
        self.assertTrue(description.skills[0].content.endswith("---\nVERSION_A"))
        created, _ = apply_proposal(original, proposal("create_skill", name="new-skill"))
        self.assertEqual(len(created.skills), 3)
        self.assertEqual(apply_proposal(original, {"action": "skip"}), (original, None))

    def test_invalid_changes_never_silently_overwrite_or_export_attachments(self):
        original, _, _ = fixture()
        for value in (None, {"action": "delete_skill"}, proposal("create_skill"), proposal(name="renamed"),
                      proposal("optimize_description"), proposal(content="---\nname: nested"),
                      proposal(files={"x.py": "print(1)"}), proposal(name="../escape")):
            with self.subTest(value=value), self.assertRaises(ValueError):
                apply_proposal(original, value, source_name="test-skill")


class SkillClawWorkflowTests(unittest.TestCase):
    def test_skill_feedback_survives_history_only_when_disclosed(self):
        from aer_core.context import ContextBuilder
        from aer_core.schemas.construction import ConstructionFeedback, ConstructionIssue
        from tests.domain_helpers import NOW
        from tests.evolution.helpers import MemoryExperience, fixture as context_fixture
        from tests.evolution.test_context import terminal
        from tests.storage.helpers import attempt
        _, stable, _, task, record, live, policy = context_fixture()
        feedback = ConstructionFeedback(component="skill", operator_id="skillclaw_skill_evolver", outcome="empty",
            issues=[ConstructionIssue(stage="judgment", code="no_skill_change", source="model_assessment", message="No reusable skill change")])
        previous = attempt(status="empty_candidates", candidate_ids=[]).replace(
            decision=EvolutionDecision(target="skill", rationale="fixed skill"), construction_feedback=feedback)
        saved = terminal(previous)
        saved = type(saved).model_validate_json(saved.model_dump_json())
        for disclosed in (False, True):
            builder = ContextBuilder(MemoryExperience([saved]), policy.replace(
                artifact_kinds=["prompt", "skill"] if disclosed else ["prompt"]))
            state = builder.build(task, record, stable, live, round_id=2, feasible_targets=["prompt"], as_of=NOW)
            self.assertEqual(state.history[0].construction_feedback, feedback if disclosed else None)

    def test_native_workflow_returns_only_complete_skill_artifact_with_receipts(self):
        original, store, request, model, result, receipt = run_workflow([
            response("Task failed after reading test-skill."), response(json.dumps(proposal()))])
        self.assertEqual(result.status, "completed", result.error)
        self.assertEqual(result.usage.model_calls, 2)
        candidate = result.candidates[0]
        self.assertEqual(set(candidate.artifact_updates), {"skill"})
        self.assertEqual(candidate.parent_version_id, request.context.stable_snapshot.version_id)
        updated = decode_library(store.get(candidate.artifact_updates["skill"]))
        self.assertEqual(updated.skills[1], original.skills[1])
        self.assertEqual(receipt["upstream_revision"], UPSTREAM_REVISION)
        self.assertEqual(receipt["session"]["_skills_referenced"], ["test-skill"])
        self.assertEqual(receipt["session"]["_summary"], "Task failed after reading test-skill.")
        self.assertEqual([call["stage"] for call in receipt["calls"]], ["summarize", "improve"])
        self.assertTrue(all(request.context.feedback.public_feedback in req.messages[-1].content for req, _ in model.requests))
        self.assertTrue(all(ctx.operation_id.startswith("exec-test/skillclaw/") for _, ctx in model.requests))
        self.assertEqual(result.construction_feedback.component, "skill")

    def test_native_no_skill_bucket_can_create_and_skip_keeps_baseline(self):
        *_, result, receipt = run_workflow([response("No skill was read."),
            response(json.dumps(proposal("create_skill", name="new-skill")))], read=False)
        self.assertEqual(result.status, "completed", result.error)
        self.assertEqual(receipt["calls"][1]["stage"], "create")
        boundary = next(event for event in receipt["events"] if event["kind"] == "no_skill_boundary")
        self.assertEqual(boundary["available_skills"], ["test-skill", "untouched"])
        self.assertFalse(boundary["unread_skills_inspected"])
        self.assertFalse(receipt["changes"][0]["missing_capability_established"])
        *_, result, receipt = run_workflow([response("Summary"), response('{"action":"skip","rationale":"No reusable change"}')])
        self.assertEqual(result.status, "empty", result.error)
        self.assertEqual(result.candidates, ())
        self.assertIn("No reusable change", json.dumps(result.construction_feedback.model_dump(mode="json")))

    def test_skip_for_agent_behavior_or_insufficient_evidence_and_missing_reason(self):
        for read, reason in ((True, "Agent ignored adequate instructions"), (False, "Insufficient evidence; description or selection may be responsible")):
            *_, result, receipt = run_workflow([response("Summary"), response(json.dumps({"action": "skip", "rationale": reason}))], read=read)
            self.assertEqual(result.status, "empty")
            self.assertEqual(result.candidates, ())
            self.assertIn(reason, result.construction_feedback.model_dump_json())
        *_, result, receipt = run_workflow([response("Summary"), response('{"action":"skip"}')])
        self.assertEqual(result.status, "error")
        self.assertIn("explicit rationale", result.error.message)

    def test_model_failures_limits_unknown_usage_and_malformed_output_fail_closed(self):
        cases = [([response("Summary"), response("not JSON")], SkillClawConfig(), "error", 2),
            ([response("Summary")], SkillClawConfig(max_model_calls=1), "budget_interrupted", 1),
            ([response("Summary", unknown=True)], SkillClawConfig(), "budget_interrupted", 1),
            ([response("partial").replace(finish_reason="length")], SkillClawConfig(), "error", 1)]
        for outputs, config, status, calls in cases:
            with self.subTest(status=status, calls=calls):
                *_, result, receipt = run_workflow(outputs, config=config)
                self.assertEqual(result.status, status, result.error)
                self.assertEqual(result.candidates, ())
                self.assertEqual(result.usage.model_calls, calls)
                self.assertIsNotNone(result.error)
        *_, result, receipt = run_workflow([], config=SkillClawConfig(max_tokens=1))
        self.assertEqual(result.status, "budget_interrupted")
        self.assertEqual(receipt["calls"], [])

    def test_optional_native_verifier_cannot_promote_a_library(self):
        *_, result, receipt = run_workflow([response("Summary"), response(json.dumps(proposal())),
            response('{"accepted":false,"score":0.1,"rationale":"Insufficient evidence"}')],
            config=SkillClawConfig(verify_candidates=True))
        self.assertEqual(result.status, "empty", result.error)
        self.assertEqual(receipt["calls"][-1]["stage"], "verify")

    def test_source_revision_and_missing_source_are_detected(self):
        with patch("plugins.evolution.skillclaw.source.subprocess.run") as run:
            run.return_value.stdout = "wrong-commit"
            with self.assertRaisesRegex(ValueError, "pinned"):
                load_upstream()
        with self.assertRaisesRegex(ValueError, "source missing"):
            load_upstream("missing-skillclaw-source")

    def test_preload_is_rejected_before_model_calls(self):
        _, store, request = fixture()
        ctx = request.context.replace(execution=request.context.execution.replace(state_summary={"injected_skills": ["test-skill"]}))
        model = RecordingModel(lambda *_: self.fail("no model call for preload"))
        result = SkillClawSkillEvolver(SkillClawConfig(), store, model).evolve(replace(request, context=ctx))
        self.assertEqual(result.status, "error")
        self.assertEqual(result.candidates, ())

    def test_later_group_failure_discards_all_changes_and_keeps_accounting(self):
        original, store, request = fixture()
        runtime = SkillRuntime.from_snapshot(request.context.stable_snapshot, store, "exec-test")
        runtime.read_skill("test-skill", call_id="read-1")
        runtime.read_skill("untouched", call_id="read-2")
        ctx = request.context.replace(execution=request.context.execution.replace(state_summary=runtime.summary("tool")))
        outputs = iter([response("Two skills were read"), response(json.dumps(proposal())), response("broken second proposal")])
        result = SkillClawSkillEvolver(SkillClawConfig(), store, RecordingModel(lambda *_: next(outputs))).evolve(
            replace(request, context=ctx))
        self.assertEqual(result.status, "error")
        self.assertEqual(result.candidates, ())
        self.assertEqual(result.usage.model_calls, 3)
        self.assertEqual(decode_library(store.get(ctx.stable_snapshot.artifacts["skill"])), original)

    def test_timeout_swallowed_by_upstream_still_stops_workflow_and_retains_usage(self):
        from aer_core.errors import OperationTimeout
        from aer_core.schemas.usage import ResourceUsage
        _, store, request = fixture()
        usage = ResourceUsage(input_tokens=10, output_tokens=0, total_tokens=10, model_calls=1, tool_calls=0,
                              elapsed_seconds=0.1, usage_complete=True, usage_sources=["simulated"])
        def fail(*_):
            raise OperationTimeout("test model deadline", stage="evolution", usage=usage)
        model = RecordingModel(fail)
        result = SkillClawSkillEvolver(SkillClawConfig(), store, model).evolve(request)
        self.assertEqual(result.status, "error")
        self.assertEqual(len(model.requests), 1)
        self.assertEqual(result.usage.total_tokens, 10)
        self.assertEqual(result.error.usage.total_tokens, 10)

    def test_multiple_calls_cannot_reuse_entry_budget(self):
        import asyncio
        from plugins.evolution.skillclaw.model_adapter import ModelAdapter
        from plugins.agents.execution import SessionRecord
        from aer_core.errors import BudgetExceeded
        _, _, request = fixture()
        class Budget:
            def remaining(self):
                return BudgetRemaining(tokens=1500, tool_calls=0, elapsed_seconds=60)
            def reserve(self, *args, **kwargs):
                raise AssertionError("The workflow reports aggregate usage")
            def settle(self, *args, **kwargs):
                raise AssertionError("The workflow reports aggregate usage")
        answer = response("Summary").replace(usage=UsageReport(input_tokens=900, output_tokens=500,
            model_calls=1, tool_calls=0, elapsed_seconds=0.01, usage_source="simulated"))
        model = RecordingModel(lambda *_: answer)
        adapter = ModelAdapter(model, replace(request.run_context, budget=Budget()), SkillClawConfig(), SessionRecord(), [], {})
        messages = [{"role": "user", "content": "Summarize"}]
        self.assertEqual(asyncio.run(adapter.chat(messages)), "Summary")
        with self.assertRaises(BudgetExceeded):
            asyncio.run(adapter.chat(messages))
        self.assertEqual(len(model.requests), 1)
