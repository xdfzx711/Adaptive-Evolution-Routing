import json
import subprocess
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

import yaml

from aer_core.errors import ContractError
from aer_core.schemas.evolution import EvolutionDecision
from aer_core.schemas.experience import AttemptFinishedEvent, CandidateEvaluatedEvent
from aer_core.schemas.proposal import EvolutionProposalRecord
from aer_core.targets import FeasibleTargets
from experiments.evolution import load_task_record, prepare_evolution
from tests.execution.helpers import RecordingModel, response


ROOT = Path(__file__).resolve().parents[2]


class EvolutionStartupTests(unittest.TestCase):
    def config(self, root, manual=False, single=False, disabled=False):
        source = "v0_1_no_evolution.yaml" if disabled else "v0_1_manual_prompt.yaml" if manual else "v0_1_fixed_prompt.yaml"
        data = yaml.safe_load((ROOT / "experiments/configs" / source).read_text(encoding="utf-8"))
        data["storage"]["root"] = str(root / "runs")
        data["agent"]["max_steps"] = 1
        if single:
            data["agent"]["plugin"] = "single_call_agent"
        if manual:
            data["routing"]["manual_decisions"] = data["routing"]["manual_decisions"][:1]
        path = root / "experiment.yaml"
        path.write_text(yaml.safe_dump(data), encoding="utf-8")
        return path

    def cli(self, *args, cwd=ROOT):
        return subprocess.run([sys.executable, str(ROOT / "main.py"), *map(str, args)], cwd=cwd,
                              capture_output=True, encoding="utf-8")

    def test_failure_routes_generates_and_saves_without_promoting_version(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            prepared = prepare_evolution(self.config(root), run_id="test")
            record = prepared.execution.run_task("dev-001")
            original = prepared.execution.storage.version_store.stable()
            old_content = prepared.execution.storage.artifact_store.get(original.artifacts["prompt"])
            proposal = prepared.propose(record, round_id=1)
            self.assertEqual(proposal.status, "finished")
            self.assertEqual(proposal.response.status, "completed")
            self.assertEqual(proposal.decision.target, "prompt")
            self.assertEqual(proposal.operator_id, "simple_prompt_evolver")
            self.assertIsNone(proposal.context.remaining_budget)
            self.assertIsNone(proposal.attempt.budget_before)
            self.assertEqual(EvolutionProposalRecord.model_validate_json(proposal.model_dump_json()), proposal)
            self.assertEqual(prepared.execution.storage.version_store.stable(), original)
            self.assertEqual(prepared.execution.storage.artifact_store.get(original.artifacts["prompt"]), old_content)
            candidate = proposal.response.candidates[0]
            self.assertNotEqual(prepared.execution.storage.artifact_store.get(candidate.artifact_updates["prompt"]), old_content)
            outcomes = [event.payload for event in prepared.execution.storage.experience_store.events() if isinstance(event, CandidateEvaluatedEvent)]
            self.assertEqual(outcomes[0].status, "not_validated")
            self.assertFalse(outcomes[0].committed)
            self.assertIsNone(outcomes[0].acceptance)
            payload = prepared.save_record(proposal)
            self.assertEqual(payload["status"], "proposal_recorded")

    def test_both_agents_can_generate_from_same_failure_and_evaluator(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for single in (False, True):
                prepared = prepare_evolution(self.config(root, single=single), run_id="single" if single else "react")
                prepared.execution.agent.model = RecordingModel(lambda request, ctx: response('{"answer":74}'))
                record = prepared.execution.run_task("dev-001")
                self.assertEqual(record.evaluation.reason_code, "incorrect_answer")
                self.assertEqual(prepared.propose(record, round_id=1).response.status, "completed")

    def test_next_round_sees_completed_history_but_cannot_reuse_round(self):
        with tempfile.TemporaryDirectory() as directory:
            prepared = prepare_evolution(self.config(Path(directory)), run_id="test")
            record = prepared.execution.run_task("dev-001")
            first = prepared.propose(record, round_id=1)
            second = prepared.propose(record, round_id=2)
            self.assertEqual(len(second.context.history), 1)
            self.assertEqual(second.context.history[0].attempt_id, first.attempt.attempt_id)
            self.assertIsNone(second.context.history[0].candidate_summaries[0].reward)
            self.assertEqual(second.context.history[0].diagnosis.reason_code, "step_limit")
            with self.assertRaises(ContractError):
                prepared.propose(record, round_id=2)

    def test_success_disabled_limit_manual_exhaustion_and_no_targets_stop_before_attempt(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            prepared = prepare_evolution(self.config(root, single=True), run_id="success")
            record = prepared.execution.run_task("dev-001")
            self.assertEqual(prepared.propose(record, round_id=1).stop_reason, "task_succeeded")
            prepared = prepare_evolution(self.config(root, disabled=True), run_id="disabled")
            record = prepared.execution.run_task("dev-001")
            self.assertEqual(prepared.propose(record, round_id=1).stop_reason, "evolution_disabled")
            self.assertEqual(prepared.execution.storage.experience_store.events(), ())
            prepared = prepare_evolution(self.config(root, manual=True), run_id="manual")
            record = prepared.execution.run_task("dev-001")
            self.assertEqual(prepared.propose(record, round_id=4).stop_reason, "max_evolution_rounds_reached")
            self.assertEqual(prepared.propose(record, round_id=2).stop_reason, "manual_plan_exhausted")
            class NoTargets:
                def feasible(self, agent, stable, allowed):
                    return FeasibleTargets(targets=[], availability=[])
            prepared = replace(prepared, targets=NoTargets())
            self.assertEqual(prepared.propose(record, round_id=1).stop_reason, "no_feasible_targets")
            self.assertEqual(prepared.execution.storage.experience_store.events(), ())

    def test_router_bad_target_is_blocked_and_recorded_without_candidate(self):
        class BadRouter:
            def select(self, context):
                return EvolutionDecision(target="memory", rationale="invalid target")
        with tempfile.TemporaryDirectory() as directory:
            prepared = prepare_evolution(self.config(Path(directory)), run_id="test")
            record = prepared.execution.run_task("dev-001")
            prepared = replace(prepared, router=BadRouter())
            proposal = prepared.propose(record, round_id=1)
            self.assertEqual(proposal.response.status, "error")
            self.assertEqual(proposal.response.candidates, ())
            events = prepared.execution.storage.experience_store.events()
            self.assertEqual(len(events), 2)
            self.assertEqual(events[-1].payload.status, "error")
            self.assertEqual(events[-1].payload.error.stage, "routing")

    def test_empty_and_error_generation_keep_experience_and_cost(self):
        with tempfile.TemporaryDirectory() as directory:
            for text, status in [('{"candidates":[]}', "empty_candidates"), ('invalid JSON', "error")]:
                root = Path(directory)
                prepared = prepare_evolution(self.config(root), run_id="empty" if status == "empty_candidates" else "error")
                record = prepared.execution.run_task("dev-001")
                prepared.resolver.resolve("prompt").operator.model = RecordingModel(lambda request, ctx: response(text))
                proposal = prepared.propose(record, round_id=1)
                self.assertEqual(proposal.attempt.status, status)
                self.assertEqual(proposal.attempt.usage.total_tokens, 5)
                finished = [event for event in prepared.execution.storage.experience_store.events() if isinstance(event, AttemptFinishedEvent)]
                self.assertEqual(len(finished), 1)
                self.assertEqual(finished[0].payload.candidate_ids, ())

    def test_hidden_feedback_never_reaches_model_or_candidate_prompt(self):
        with tempfile.TemporaryDirectory() as directory:
            prepared = prepare_evolution(self.config(Path(directory)), run_id="privacy")
            record = prepared.execution.run_task("dev-001")
            canary = "PRIVATE_SCORER_LABEL_c8e712"
            record = record.replace(evaluation=record.evaluation.replace(feedback=canary),
                feedback=record.feedback.replace(public_feedback=canary),
                diagnosis=record.diagnosis.replace(explanation=canary))
            operator = prepared.resolver.resolve("prompt").operator
            original_model = operator.model
            model = RecordingModel(original_model.generate)
            operator.model = model
            proposal = prepared.propose(record, round_id=1)
            self.assertEqual(proposal.response.status, "completed")
            self.assertNotIn(canary, model.requests[0][0].model_dump_json())
            content = prepared.execution.storage.artifact_store.get(proposal.response.candidates[0].artifact_updates["prompt"])
            self.assertNotIn(canary.encode(), content)

    def test_other_run_or_stale_execution_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            prepared = prepare_evolution(self.config(Path(directory)), run_id="test")
            record = prepared.execution.run_task("dev-001")
            with self.assertRaises(ContractError):
                prepared.propose(record.replace(run_id="other-run"), round_id=1)
            result = record.execution.replace(agent_version_id="other-version")
            evaluation = record.evaluation.replace(agent_version_id="other-version")
            with self.assertRaises(ContractError):
                prepared.propose(record.replace(execution=result, evaluation=evaluation), round_id=1)

    def test_cli_execute_report_then_propose_from_other_working_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = self.config(root)
            prepared = prepare_evolution(config, run_id="cli")
            report = prepared.execution.save_record(prepared.execution.run_task("dev-001"))
            input_path = root / "execution.json"
            input_path.write_text(json.dumps(report), encoding="utf-8")
            self.assertEqual(load_task_record(input_path).run_id, "cli")
            result = self.cli("propose-prompt", config, input_path, "--run-id", "cli", "--round-id", 1, cwd=root)
            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["record"]["response"]["status"], "completed")
            original = input_path.read_bytes()
            result = self.cli("propose-prompt", config, input_path, "--run-id", "cli", "--round-id", 2, "--output", input_path, cwd=root)
            self.assertEqual(result.returncode, 2)
            self.assertEqual(input_path.read_bytes(), original)
