import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

from aer_core.errors import EvaluationError, ValidationError
from aer_core.schemas.evolution import EvolutionResponse
from aer_core.schemas.experience import CandidateEvaluatedEvent
from aer_core.schemas.paired import ValidationBatchResult
from experiments.evolution import prepare_evolution
from experiments.validation import load_proposal, prepare_validation
from plugins.providers.scripted import ScriptedModel
from tests.execution.helpers import MODEL, RecordingModel, response


ROOT = Path(__file__).resolve().parents[2]


class ValidationStartupTests(unittest.TestCase):
    def config(self, root, single=False):
        data = yaml.safe_load((ROOT / "experiments/configs/v0_1_fixed_prompt.yaml").read_text(encoding="utf-8"))
        data["storage"]["root"] = str(root / "runs")
        data["agent"]["max_steps"] = 1
        data["evolution"]["max_candidates_per_round"] = 2
        if single:
            data["agent"]["plugin"] = "single_call_agent"
        path = root / "config.yaml"
        path.write_text(yaml.safe_dump(data), encoding="utf-8")
        return path

    def setup_batch(self, root, single=False, count=1):
        path = self.config(root, single=single)
        generation = prepare_evolution(path, run_id="validation-test")
        generation.execution.agent.model = RecordingModel(lambda req, ctx: response('{"answer":-999}'))
        record = generation.execution.run_task("dev-001")
        generation.resolver.resolve("prompt").operator.model = RecordingModel(lambda req, ctx:
            response(json.dumps({"candidates": [{"prompt": f"IMPROVED reusable instructions {index}"} for index in range(count)]})))
        proposal = generation.propose(record, round_id=1)
        prepared = prepare_validation(path, run_id="validation-test")
        scripted = ScriptedModel(MODEL)
        model = RecordingModel(lambda req, ctx: scripted.generate(req.replace(tools=[]), ctx) if req.messages[0].content.startswith("IMPROVED")
                               else response('{"answer":-999}'))
        prepared.execution.agent.model = model
        return path, generation, record, proposal, prepared, model

    def test_both_agents_validate_accept_select_save_and_leave_stable_unchanged(self):
        for single in (False, True):
            with self.subTest(single=single), tempfile.TemporaryDirectory() as directory:
                path, generation, record, proposal, prepared, model = self.setup_batch(Path(directory), single=single)
                stable = prepared.execution.storage.version_store.stable()
                result = prepared.validate(proposal)
                self.assertEqual(result.selected_candidate_id, proposal.response.candidates[0].candidate_id)
                self.assertEqual(result.reports[0].status, "complete")
                self.assertTrue(result.reports[0].local_metrics.repaired)
                self.assertEqual(result.reports[0].candidate_metrics.score, 1)
                outcome = result.outcomes[0]
                self.assertTrue(outcome.acceptance.accepted)
                self.assertTrue(outcome.selected)
                self.assertFalse(outcome.committed)
                self.assertEqual(prepared.execution.storage.version_store.stable(), stable)
                saved = prepared.execution.storage.version_store.load_validation(outcome.validation_ref)
                self.assertEqual(saved, result.reports[0])
                self.assertEqual(ValidationBatchResult.model_validate_json(result.model_dump_json()), result)
                self.assertEqual(prepared.save_record(result)["status"], "validation_recorded")
                calls = len(model.requests)
                self.assertEqual(prepared.validate(proposal), result)
                self.assertEqual(len(model.requests), calls)
                second = generation.propose(record, round_id=2)
                historical = second.context.history[0].candidate_summaries[0]
                self.assertEqual(historical.reward, 1)
                self.assertTrue(historical.accepted)
                self.assertTrue(historical.selected)
                self.assertFalse(historical.committed)

    def test_multicandidate_id_join_accepted_vs_selected_and_cost(self):
        with tempfile.TemporaryDirectory() as directory:
            _, _, _, proposal, prepared, model = self.setup_batch(Path(directory), count=2)
            harness = prepared.built_plugins.instances["validation"]
            original = harness.validate
            harness.validate = lambda *args: tuple(reversed(original(*args)))
            result = prepared.validate(proposal)
            self.assertEqual(len(model.requests), 27)
            self.assertEqual(result.usage.model_calls, 27)
            self.assertEqual(sum(outcome.selected for outcome in result.outcomes), 1)
            self.assertTrue(all(outcome.acceptance.accepted for outcome in result.outcomes))
            self.assertEqual(result.selected_candidate_id, min(item.candidate_id for item in proposal.response.candidates))

    def test_incomplete_validation_persists_failure_and_does_not_reward(self):
        with tempfile.TemporaryDirectory() as directory:
            _, _, _, proposal, prepared, _ = self.setup_batch(Path(directory))
            evaluator = prepared.execution.benchmark.evaluator
            original = evaluator.evaluate
            # Force a real scorer failure on any candidate task, avoiding task-name assumptions.
            def failed(task, execution, spec, context):
                if execution.agent_version_id != proposal.stable_version_id:
                    raise EvaluationError("SCORER_FAILURE_CANARY", stage="evaluation")
                return original(task, execution, spec, context)
            evaluator.evaluate = failed
            result = prepared.validate(proposal)
            self.assertIsNone(result.selected_candidate_id)
            self.assertEqual(result.reports[0].status, "error")
            self.assertIsNone(result.outcomes[0].acceptance.reward)
            self.assertEqual(result.outcomes[0].status, "incomplete")
            self.assertGreater(result.usage.total_tokens, 0)
            self.assertEqual(prepared.execution.storage.version_store.stable().version_id, proposal.stable_version_id)

    def test_validation_labels_never_enter_model_or_next_evolution_context(self):
        with tempfile.TemporaryDirectory() as directory:
            _, generation, record, proposal, prepared, model = self.setup_batch(Path(directory))
            canary = "PRIVATE_VALIDATION_LABEL_a39f8b"
            evaluator = prepared.execution.benchmark.evaluator
            original = evaluator.evaluate
            evaluator.evaluate = lambda *args: original(*args).replace(feedback=canary)
            result = prepared.validate(proposal)
            self.assertTrue(result.outcomes[0].acceptance.accepted)
            self.assertTrue(all(canary not in req.model_dump_json() for req, _ in model.requests))
            next_proposal = generation.propose(record, round_id=2)
            self.assertNotIn(canary, next_proposal.context.model_dump_json())
            self.assertNotIn("scoring_specs", next_proposal.context.model_dump_json())

    def test_invalid_artifact_is_rejected_with_experience_and_no_execution(self):
        with tempfile.TemporaryDirectory() as directory:
            _, _, _, proposal, prepared, model = self.setup_batch(Path(directory))
            ref = proposal.response.candidates[0].artifact_updates["prompt"]
            (prepared.execution.storage.run_root / ref.uri).write_bytes(b"corrupted")
            result = prepared.validate(proposal)
            self.assertEqual(result.reports, ())
            self.assertEqual(result.outcomes[0].acceptance.reason, "invalid_candidate")
            self.assertIsNotNone(result.outcomes[0].validation_error)
            self.assertIsNone(result.selected_candidate_id)
            self.assertEqual(model.requests, [])

    def test_foreign_proposal_and_stale_parent_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            _, _, _, proposal, prepared, _ = self.setup_batch(Path(directory))
            with self.assertRaises(ValidationError):
                prepared.validate(proposal.replace(run_id="foreign", attempt=proposal.attempt.replace(run_id="foreign")))
            with self.assertRaises(ValidationError):
                prepared.validate(proposal.replace(response=proposal.response.replace(status="empty", candidates=[])))
            with self.assertRaises(ValidationError):
                prepared.validate(proposal.replace(attempt=proposal.attempt.replace(operator_id="foreign")))

    def test_cli_roundtrip_from_other_directory_and_output_protection(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path, generation, _, proposal, _, _ = self.setup_batch(root)
            input_path = root / "proposal.json"
            input_path.write_text(json.dumps(generation.save_record(proposal)), encoding="utf-8")
            self.assertEqual(load_proposal(input_path), proposal)
            result = subprocess.run([sys.executable, str(ROOT / "main.py"), "validate-proposal", str(path), str(input_path),
                "--run-id", "validation-test"], cwd=root, encoding="utf-8", capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["status"], "validation_recorded")
            self.assertIsNone(payload["record"]["selected_candidate_id"])
            self.assertEqual(payload["record"]["outcomes"][0]["acceptance"]["reason"], "no_improvement")
            original = input_path.read_bytes()
            protected = subprocess.run([sys.executable, str(ROOT / "main.py"), "validate-proposal", str(path), str(input_path),
                "--run-id", "validation-test", "--output", str(input_path)], cwd=root, encoding="utf-8", capture_output=True)
            self.assertEqual(protected.returncode, 2)
            self.assertEqual(input_path.read_bytes(), original)
