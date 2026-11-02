"""Wiring tests use controlled replies; they do not measure model routing accuracy."""

import json
from contextlib import redirect_stdout
import io
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import yaml

from experiments.check_skill_routing import CASES, case_context, check_cases, main as check_main
from experiments.evolution import assemble_evolution
from experiments.execution import prepare_execution
from experiments.orchestration import prepare_orchestrator
from plugins.routers.skill_guidance import SKILL_ROUTING_GUIDANCE
from plugins.skills.library import decode_library
from tests.execution.helpers import MODEL, RecordingModel, response
from tests.execution.test_codex import FAKE
from tests.integration.test_codex_routing import ROOT
from tests.integration.test_codex_skillclaw import qwen_model


def adaptive_config(root, rounds=1, mode="skill-evolution"):
    data = yaml.safe_load((ROOT / "experiments/configs/v0_1_codex_skillclaw_router.yaml").read_text(encoding="utf-8"))
    data["storage"]["root"] = str(root / "runs")
    data["benchmark"]["plugin"] = "toy_tasks"
    data["providers"]["model"] = MODEL.model_dump(mode="json")
    data["assessment"]["diagnoser"] = "lightweight_diagnoser"
    data["agent"]["config"].update(command=[sys.executable, str(FAKE), "--fixture-mode", mode],
        initial_model="gpt-5.6-sol", workspace_root=str(root / "workspaces"))
    data["agent"]["config"]["skills"]["initial_library"] = str(ROOT / "experiments/skills")
    data["limits"]["max_evolution_rounds"] = rounds
    path = root / "config.yaml"
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return path


class SkillRoutingTests(unittest.TestCase):
    def test_router_check_cli_writes_a_report_and_returns_failure_for_wrong_choices(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = adaptive_config(root)
            from aer_core.schemas.evolution import EvolutionDecision
            answer = EvolutionDecision(target="model", rationale="Controlled negative test")
            with patch("plugins.routers.llm.LLMRouter.select", return_value=answer), redirect_stdout(io.StringIO()):
                code = check_main([str(path)])
            self.assertEqual(code, 1)
            report = json.loads(next((root / "runs").glob("*/skill_routing_check.json")).read_text(encoding="utf-8"))
            self.assertEqual((report["matched"], report["scored"], report["errors"]), (0, 4, 0))
            self.assertEqual(report["manual_review"], 2)

    def test_all_four_targets_are_feasible_and_router_can_select_each(self):
        with tempfile.TemporaryDirectory() as directory:
            evolution = assemble_evolution(prepare_execution(adaptive_config(Path(directory))))
            stable = evolution.execution.storage.version_store.stable()
            self.assertEqual(set(evolution.feasible_targets(stable)), {"prompt", "tool", "skill", "model"})
            cases = json.loads(CASES.read_text(encoding="utf-8"))
            context = case_context(cases[0], evolution)
            for target in context.feasible_targets:
                with self.subTest(target=target):
                    model = RecordingModel(lambda *_: response(json.dumps({"target": target, "rationale": "controlled protocol reply"})))
                    evolution.router.model = model
                    decision = evolution.router.select(context)
                    self.assertEqual(decision.target, target)
                    request = model.requests[0][0]
                    self.assertTrue(request.messages[0].content.endswith(SKILL_ROUTING_GUIDANCE))
                    body = json.loads(request.messages[1].content)
                    self.assertEqual(set(body["context"]["target_descriptions"]), {"prompt", "tool", "skill", "model"})
                    self.assertIn("no-skill branch", body["context"]["target_descriptions"]["skill"])
                    self.assertIn("OS-Copilot", body["context"]["target_descriptions"]["tool"])
            without = stable.replace(artifacts={k: v for k, v in stable.artifacts.items() if k != "skill"})
            self.assertNotIn("skill", evolution.feasible_targets(without))

    def test_router_only_check_keeps_labels_private_records_unknowns_and_does_not_execute(self):
        with tempfile.TemporaryDirectory() as directory:
            evolution = assemble_evolution(prepare_execution(adaptive_config(Path(directory))))
            stable = evolution.execution.storage.version_store.stable()
            cases = json.loads(CASES.read_text(encoding="utf-8"))
            answers = iter(["skill", "prompt", "tool", "prompt", "model", "skill"])
            model = RecordingModel(lambda *_: response(json.dumps({"target": next(answers), "rationale": "controlled reply, not measured accuracy"})))
            evolution.router.model = model
            with patch.object(evolution.execution.agent, "run", side_effect=AssertionError("no Agent run")), \
                 patch.object(evolution.resolver.resolve("skill").operator, "evolve", side_effect=AssertionError("no evolution")):
                report = check_cases(evolution, cases)
            self.assertEqual((report["matched"], report["scored"], report["manual_review"], report["errors"]), (4, 4, 2, 0))
            self.assertEqual(len(model.requests), 6)
            self.assertEqual(evolution.execution.storage.version_store.stable(), stable)
            for (request, _), case in zip(model.requests, cases):
                wire = request.model_dump_json()
                self.assertNotIn("expected_targets", wire)
                self.assertNotIn(case["review_note"], wire)
                self.assertNotIn("run_context", wire)
                body = json.loads(request.messages[1].content)
                state = body["context"]["execution"]["state_summary"]
                self.assertTrue(state["synthetic"])
                if case["read"] is None:
                    self.assertNotIn("read_skills", state)
                    self.assertNotIn("skill_discovered", state)
                else:
                    self.assertEqual(bool(state["read_skills"]), case["read"])
            self.assertTrue(all(item["record_ref"] for item in report["cases"]))
            evolution.router.model = RecordingModel(lambda *_: response('{"target":"memory","rationale":"invalid"}'))
            failed = check_cases(evolution, cases[:1])
            self.assertEqual((failed["matched"], failed["errors"]), (0, 1))
            self.assertIsNotNone(failed["cases"][0]["error"]["evidence_ref"])

    def test_adaptive_skill_route_runs_native_pipeline_validates_reads_and_promotes_only_skill(self):
        with tempfile.TemporaryDirectory() as directory:
            startup = prepare_orchestrator(adaptive_config(Path(directory)), console=False)
            task = startup.prepare_task("dev-001")
            stable = task.execution.storage.version_store.stable()
            original = decode_library(task.execution.storage.artifact_store.get(stable.artifacts["skill"])).skills[0]
            calls = []
            model = qwen_model([
                '{"target":"skill","rationale":"Inspect reusable guidance from the read skill"}',
                "The visible execution read the skill and failed its public check.",
                json.dumps({"action": "improve_skill", "rationale": "Improve reusable guidance", "skill": {
                    "name": original.name, "description": original.description,
                    "content": "Follow the public output contract. AER_SKILL_IMPROVED"}})], calls)
            task.evolution.router.model = model
            task.evolution.resolver.resolve("skill").operator.model = model
            with patch.object(task.evolution.resolver.resolve("prompt").operator, "evolve", side_effect=AssertionError("unselected prompt")), \
                 patch.object(task.evolution.resolver.resolve("tool").operator, "evolve", side_effect=AssertionError("unselected tool")), \
                 patch.object(task.evolution.resolver.resolve("model").operator, "evolve", side_effect=AssertionError("unselected model")):
                result = task.run()
            self.assertEqual(result.stop_reason, "task_succeeded", result.error)
            self.assertEqual(len(calls), 3)
            committed = task.execution.storage.version_store.stable()
            self.assertNotEqual(committed.artifacts["skill"], stable.artifacts["skill"])
            for kind in set(stable.artifacts) - {"skill"}:
                self.assertEqual(committed.artifacts[kind], stable.artifacts[kind])
            record = json.loads(next(startup.execution.storage.run_root.glob("rounds/*/round-001/record.json")).read_text(encoding="utf-8"))
            self.assertEqual(record["routing"]["operator"], "skillclaw_skill_evolver")
            self.assertTrue(record["candidates"][0]["committed"])
            for pair in record["candidates"][0]["validation"]["tasks"]:
                self.assertIsNotNone(pair["skill_usage"])
                self.assertNotEqual(pair["baseline"]["execution"]["state_summary"]["skill_reads"][0]["content_digest"],
                                    pair["candidate"]["execution"]["state_summary"]["skill_reads"][0]["content_digest"])

    def test_skip_is_sent_back_to_router_before_next_competing_target(self):
        with tempfile.TemporaryDirectory() as directory:
            startup = prepare_orchestrator(adaptive_config(Path(directory), rounds=2), console=False)
            task = startup.prepare_task("dev-001")
            stable = task.execution.storage.version_store.stable()
            routes = iter(["skill", "prompt"])
            routing = RecordingModel(lambda *_: response(json.dumps({"target": next(routes), "rationale": "Use current evidence and prior skip"})))
            task.evolution.router.model = routing
            calls = []
            task.evolution.resolver.resolve("skill").operator.model = qwen_model([
                "Correct skill guidance was ignored by the Agent.",
                '{"action":"skip","rationale":"Existing guidance is adequate; the Agent ignored it."}'], calls)
            task.evolution.resolver.resolve("prompt").operator.model = RecordingModel(lambda *_: response('{"candidates":[]}'))
            result = task.run()
            self.assertEqual(result.stop_reason, "max_evolution_rounds_reached", result.error)
            self.assertEqual(task.execution.storage.version_store.stable(), stable)
            self.assertEqual(len(routing.requests), 2)
            second = json.loads(routing.requests[1][0].messages[1].content)
            feedback = second["context"]["history"][0]["construction_feedback"]
            self.assertEqual(feedback["component"], "skill")
            self.assertIn("Agent ignored it", json.dumps(feedback))


if __name__ == "__main__":
    unittest.main()
