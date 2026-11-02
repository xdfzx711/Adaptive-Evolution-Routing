import json
from pathlib import Path
import tempfile
import unittest

import httpx
from pydantic import SecretStr
import yaml

from aer_core.config.models import ModelProviderSettings
from experiments.orchestration import prepare_orchestrator
from plugins.providers.qwen import QwenCredentials, QwenModel
from plugins.skills.library import decode_library
from tests.integration.test_codex_routing import config, ROOT


def skill_config(root, mode="skill-evolution"):
    path = config(root, mode=mode)
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    data["assessment"]["diagnoser"] = "lightweight_diagnoser"
    data["evolution"]["mode"] = "fixed"
    data["routing"] = {"plugin": "fixed_router", "fixed_target": "skill", "manual_decisions": []}
    data["targets"] = {"allowed": ["skill"], "default_operators": {"skill": "skillclaw_skill_evolver"},
                       "operator_configs": {"skill": {"max_tokens": 48000}}}
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return path


def qwen_model(outputs, calls):
    responses = iter(outputs)
    def handle(request):
        calls.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": next(responses)},
            "finish_reason": "stop"}], "usage": {"prompt_tokens": 50, "completion_tokens": 20}})
    return QwenModel(ModelProviderSettings(plugin="qwen_model", model_id="qwen-fixture", config={"response_format": None}),
        QwenCredentials(SecretStr("local-test-only"), "https://fixture.invalid/v1"), transport=httpx.MockTransport(handle))


class CodexSkillClawIntegrationTests(unittest.TestCase):
    def test_native_pipeline_qwen_interface_codex_reads_and_aer_promotion(self):
        for mode, commits in (("skill-evolution", True), ("skill-evolution-reject", False)):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                startup = prepare_orchestrator(skill_config(Path(directory), mode), run_id="skill-test", console=False)
                task = startup.prepare_task("dev-001")
                store = task.execution.storage.artifact_store
                initial = task.execution.storage.version_store.stable()
                original = decode_library(store.get(initial.artifacts["skill"]))
                original_skill = next(item for item in original.skills if item.name == "task-output-contract")
                calls = []
                task.evolution.resolver.resolve("skill").operator.model = qwen_model([
                    "The visible task execution read task-output-contract and failed its public check.",
                    json.dumps({"action": "improve_skill", "rationale": "Improve reusable output guidance", "skill": {
                        "name": original_skill.name, "description": original_skill.description,
                        "content": "Follow the public output contract. AER_SKILL_IMPROVED"}})], calls)
                result = task.run()
                self.assertEqual(result.stop_reason, "task_succeeded" if commits else "max_evolution_rounds_reached")
                stable = task.execution.storage.version_store.stable()
                if commits:
                    self.assertNotEqual(stable.artifacts["skill"], initial.artifacts["skill"])
                    for kind in set(initial.artifacts) - {"skill"}:
                        self.assertEqual(stable.artifacts[kind], initial.artifacts[kind])
                    self.assertEqual(stable.config, initial.config)
                else:
                    self.assertEqual(stable, initial)
                record = json.loads(next(startup.execution.storage.run_root.glob("rounds/*/round-001/record.json")).read_text(encoding="utf-8"))
                self.assertEqual(record["candidates"][0]["committed"], commits)
                receipt = record["generation"]["skill_evolution"]
                self.assertEqual(receipt["status"], "completed")
                self.assertEqual(receipt["session"]["_skills_referenced"], ["task-output-contract"])
                self.assertIsNotNone(receipt["evidence"]["skill_reads"][0]["event_seq"])
                self.assertEqual(len(calls), 2)
                self.assertTrue(all(call["model"] == "qwen-fixture" and "response_format" not in call for call in calls))
                for pair in record["candidates"][0]["validation"]["tasks"]:
                    old_state = pair["baseline"]["execution"]["state_summary"]
                    new_state = pair["candidate"]["execution"]["state_summary"]
                    self.assertEqual(old_state["execution_model"], "gpt-5.6-sol")
                    self.assertEqual(new_state["execution_model"], "gpt-5.6-sol")
                    self.assertNotEqual(old_state["skill_library_artifact_id"], new_state["skill_library_artifact_id"])
                    usage = pair["skill_usage"]
                    self.assertTrue(usage["integrity_ok"])
                    skill = usage["skills"]["task-output-contract"]
                    self.assertTrue(skill["candidate"]["discovered"])
                    self.assertTrue(skill["candidate"]["read_verified"])
                    self.assertTrue(skill["candidate"]["reads"][0]["matches_expected"])
                    self.assertEqual(skill["body_attribution"], "verified_exposure_only_not_causal")
                report = (startup.execution.storage.run_root / "evolution.txt").read_text(encoding="utf-8")
                self.assertIn("SkillClaw", report)
                self.assertIn("generation.skill_evolution", report)
                self.assertIn("技能执行证据", report)

    def test_sample_config_explicitly_selects_fixed_skill_and_keeps_agent_model_independent(self):
        from experiments.configuration import load_experiment_config
        settings = load_experiment_config(ROOT / "experiments/configs/v0_1_codex_skillclaw.yaml")
        self.assertEqual(settings.config.routing.fixed_target, "skill")
        self.assertEqual(settings.config.providers.model.plugin, "qwen_model")
        self.assertEqual(settings.config.agent.plugin, "codex_cli_agent")
