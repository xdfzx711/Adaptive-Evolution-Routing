from dataclasses import replace
from pathlib import Path
import sys
import tempfile
import unittest

from aer_core.config.models import AgentSettings
from aer_core.schemas.references import AgentSnapshot
from aer_core.schemas.task import TaskView
from plugins.agents.codex import CodexCliAgent, CodexConfig, controlled_config
from plugins.agents.model_selection import ModelSelection
from plugins.skills.library import encode_library
from plugins.workspace.local import EmptyWorkspace
from plugins.workspace.collectors import GitPatchCollector, PatchCollectorSettings
from plugins.workspace.git import git
from plugins.mocks import MockArtifactStore
from tests.execution.helpers import context
from tests.skills.test_execution import library


class GitWorkspace(EmptyWorkspace):
    def prepare(self, task, directory, run_ctx):
        handle = super().prepare(task, directory, run_ctx)
        (directory / "module.py").write_text("answer = 1\n", encoding="utf-8")
        git(directory, "init", "--quiet", timeout=10)
        git(directory, "add", ".", timeout=10)
        git(directory, "-c", "user.name=AER", "-c", "user.email=aer@localhost", "commit", "--quiet", "-m", "base", timeout=10)
        revision = git(directory, "rev-parse", "HEAD", timeout=10).strip()
        return replace(handle, baseline_revision=revision, source_revision=revision)


class CodexSkillTests(unittest.TestCase):
    def test_skill_resources_do_not_enter_repository_patch(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fake = Path(__file__).resolve().parents[1] / "execution/fake_codex.py"
            config = CodexConfig(command=[sys.executable, str(fake), "--fixture-mode", "skills-patch"],
                                 workspace_root=str(root / "workspaces"), skills={})
            store = MockArtifactStore()
            snapshot = AgentSnapshot(version_id="skill-patch", agent_plugin_id="codex_cli_agent",
                config=controlled_config(config), artifacts={"prompt": store.put(b"Fix the issue", "prompt"),
                    "model": store.put(ModelSelection(model_id="gpt-6.1-sol").model_dump_json().encode(), "model"),
                    "skill": store.put(encode_library(library()), "skill")})
            agent = CodexCliAgent(AgentSettings(plugin="codex_cli_agent", initial_prompt="Fix", initial_snapshot_ref=None,
                config=config.model_dump(mode="json")), snapshot, store, workspace_provider=GitWorkspace(),
                result_collector=GitPatchCollector(PatchCollectorSettings(), store))
            result = agent.run(TaskView(task_id="patch", benchmark_id="test", input={}), snapshot, context())
            self.assertEqual(result.status, "completed", result.error)
            self.assertEqual(result.output["changed_files"], ("module.py",))
            self.assertIn("+answer = 2", result.output["patch"])
            self.assertNotIn("SKILL.md", result.output["patch"])
            self.assertNotIn("library.json", result.output["patch"])
            self.assertEqual(result.state_summary["read_skills"], ("test-skill",))
            self.assertEqual(result.usage.tool_calls, 1)
            self.assertEqual(agent.capabilities().skill_delivery.transport, "mcp")

    def test_missing_service_is_error_but_unused_service_is_valid(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fake = Path(__file__).resolve().parents[1] / "execution/fake_codex.py"
            for mode, expected in (("skills-unused", "completed"), ("skills-ignored", "error")):
                config = CodexConfig(command=[sys.executable, str(fake), "--fixture-mode", mode],
                                     workspace_root=str(root / "workspaces"), skills={})
                store = MockArtifactStore()
                snapshot = AgentSnapshot(version_id=mode, agent_plugin_id="codex_cli_agent",
                    config=controlled_config(config), artifacts={"prompt": store.put(b"Return JSON", "prompt"),
                        "model": store.put(ModelSelection(model_id="gpt-6.1-sol").model_dump_json().encode(), "model"),
                        "skill": store.put(encode_library(library()), "skill")})
                agent = CodexCliAgent(AgentSettings(plugin="codex_cli_agent", initial_prompt="Return JSON", initial_snapshot_ref=None,
                    config=config.model_dump(mode="json")), snapshot, store)
                task = TaskView(task_id="test", benchmark_id="test", input={"output_schema": {"type": "object"}})
                result = agent.run(task, snapshot, context())
                self.assertEqual(result.status, expected, result.error)
                self.assertEqual(result.state_summary["read_skills"], ())
