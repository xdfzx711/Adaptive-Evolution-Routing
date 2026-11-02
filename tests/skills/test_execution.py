import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import tempfile
import unittest

import yaml
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from aer_core.config.models import AgentSettings
from aer_core.schemas.providers import ToolCall
from aer_core.schemas.references import AgentSnapshot
from aer_core.schemas.skills import SkillLibrary
from experiments.storage import prepare_storage
from plugins.agents.react import ReActAgent, SingleCallAgent, LocalAgentSettings
from plugins.skills.library import parse_skill, encode_library, decode_library, import_library
from plugins.skills.runtime import SkillRuntime
from plugins.skills.codex import CodexSkillSession
from plugins.skills.library import SkillsConfig, initial_skill_artifacts
from plugins.providers.tools import DeterministicTools
from plugins.benchmarks.toy_tasks import ToyBenchmark
from plugins.mocks import MockArtifactStore
from tests.execution.helpers import MODEL, TOOLS, RecordingModel, context, response


ROOT = Path(__file__).resolve().parents[2]


def library(marker="VERSION_A"):
    return SkillLibrary(skills=[parse_skill("---\nname: test-skill\ndescription: Read for the public skill probe.\n---\n" + marker)])


def fixture(model, *, single=False):
    store = MockArtifactStore()
    name = "single_call_agent" if single else "react_agent"
    skill_settings = {"mode": "preload", "preload": ["test-skill"]} if single else {"mode": "on_demand", "preload": []}
    snapshot = AgentSnapshot(version_id="A", agent_plugin_id=name,
        config={"max_steps": 1 if single else 3, "model": MODEL.model_dump(mode="json"),
                "tools": TOOLS.model_dump(mode="json"), "skills": skill_settings},
        artifacts={"prompt": store.put(b"Return JSON", "prompt"), "skill": store.put(encode_library(library()), "skill")})
    settings = AgentSettings(plugin=name, initial_prompt="Return JSON", initial_snapshot_ref=None,
                             max_steps=1 if single else 3, config={"skills": skill_settings})
    cls = SingleCallAgent if single else ReActAgent
    agent = cls(settings, snapshot, store, model, None if single else DeterministicTools(TOOLS))
    return agent, snapshot, store


def reading_model():
    def respond(request, _):
        outputs = [message for message in request.messages if message.role == "tool"]
        if not outputs:
            return response(tool_calls=[ToolCall(call_id="read-1", name="read_skill", arguments={"name": "test-skill"})])
        content = json.loads(outputs[-1].content)["output"]["content"]
        return response(json.dumps({"marker": content.splitlines()[-1]}))
    return RecordingModel(respond)


class SkillExecutionTests(unittest.TestCase):
    def setUp(self):
        benchmark = ToyBenchmark()
        self.task = benchmark.task_view(benchmark.tasks("dev")[0])

    def test_react_reads_passed_snapshot_and_public_receipts(self):
        model = reading_model()
        agent, first, store = fixture(model)
        second = first.replace(version_id="B", parent_version_id="A", artifacts={**first.artifacts,
            "skill": store.put(encode_library(library("VERSION_B")), "skill")})
        for index, snapshot in enumerate((first, second, first)):
            result = agent.run(self.task, snapshot, context(operation_id=f"exec-{index}"))
            self.assertEqual(result.status, "completed", result.error)
            self.assertEqual(result.output["marker"], "VERSION_" + snapshot.version_id)
            self.assertEqual(result.state_summary["read_skills"], ("test-skill",))
            receipt = result.state_summary["skill_reads"][0]
            self.assertEqual(receipt["snapshot_version_id"], snapshot.version_id)
            self.assertEqual(receipt["execution_operation_id"], f"exec-{index}")
            self.assertEqual(result.usage.tool_calls, 1)
            self.assertEqual(result.usage.model_calls, 2)
        self.assertNotIn("VERSION_A", model.requests[0][0].messages[0].content)
        self.assertEqual(agent.capabilities().skill_delivery.transport, "tool")
        self.assertNotIn("skill", agent.capabilities().supported_targets)

    def test_concurrent_executions_do_not_share_read_receipts(self):
        agent, snapshot, _ = fixture(reading_model())
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda index: agent.run(self.task, snapshot, context(operation_id=f"parallel-{index}")), range(2)))
        for index, result in enumerate(results):
            self.assertEqual(len(result.state_summary["skill_reads"]), 1)
            self.assertEqual(result.state_summary["skill_reads"][0]["execution_operation_id"], f"parallel-{index}")

    def test_single_call_preload_is_not_reported_as_agent_read(self):
        model = RecordingModel(lambda request, ctx: response('{"ok":true}'))
        agent, snapshot, _ = fixture(model, single=True)
        result = agent.run(self.task, snapshot, context())
        self.assertEqual(result.status, "completed", result.error)
        self.assertIn("VERSION_A", model.requests[0][0].messages[0].content)
        self.assertEqual(model.requests[0][0].tools, ())
        self.assertEqual(result.state_summary["injected_skills"], ("test-skill",))
        self.assertEqual(result.state_summary["read_skills"], ())
        self.assertEqual(result.usage.tool_calls, 0)
        with self.assertRaises(ValueError):
            LocalAgentSettings(plugin="single_call_agent", max_steps=1, initial_prompt="x", initial_snapshot_ref=None,
                               config={"skills": {}})

    def test_unused_and_missing_skills_are_distinct(self):
        agent, snapshot, store = fixture(RecordingModel(lambda request, ctx: response('{"ok":true}')))
        result = agent.run(self.task, snapshot, context())
        self.assertEqual(result.state_summary["read_skills"], ())
        runtime = SkillRuntime.from_snapshot(snapshot, store, "probe")
        result = runtime.invoke(ToolCall(call_id="missing", name="read_skill", arguments={"name": "unknown"}), context())
        self.assertEqual(result.status, "error")
        self.assertEqual(runtime.summary("tool")["read_skills"], [])
        self.assertEqual(runtime.reads[0]["status"], "error")

    def test_empty_library_is_a_real_artifact(self):
        artifacts = initial_skill_artifacts(SkillsConfig())
        self.assertEqual(set(artifacts), {"skill"})
        self.assertEqual(decode_library(artifacts["skill"]).skills, ())
        self.assertEqual(initial_skill_artifacts(None), {})

    def test_skill_content_and_runtime_version_partition_execution_cache(self):
        from aer_core.cached_executor import CachedTaskExecutor
        from plugins.validation.cache import LocalValidationCache
        from tests.validation.helpers import ScoreExecutor, fixture as validation_fixture
        with tempfile.TemporaryDirectory() as directory:
            artifacts, _, snapshot, _, plan = validation_fixture(directory)
            first = snapshot.replace(artifacts={**snapshot.artifacts,
                "skill": artifacts.put(encode_library(library()), "skill")})
            second = first.replace(version_id="skill-B", parent_version_id=first.version_id,
                artifacts={**first.artifacts, "skill": artifacts.put(encode_library(library("VERSION_B")), "skill")})
            executor = ScoreExecutor()
            cache = LocalValidationCache(directory)
            cached = CachedTaskExecutor(executor, cache, {"skill_runtime_version": "1"})
            self.assertFalse(cached.execute(plan.current_task, first, context()).cache_hit)
            self.assertFalse(cached.execute(plan.current_task, second, context()).cache_hit)
            self.assertTrue(cached.execute(plan.current_task, first, context()).cache_hit)
            self.assertFalse(CachedTaskExecutor(executor, cache, {"skill_runtime_version": "2"}).execute(
                plan.current_task, first, context()).cache_hit)
            self.assertEqual(len(executor.calls), 3)

    def test_public_feedback_preserves_declared_skill_evidence(self):
        from aer_core.feedback import PublicFeedbackBuilder
        from aer_core.schemas.feedback import ContextPolicy
        from aer_core.schemas.execution import EvaluationResult
        agent, snapshot, _ = fixture(reading_model())
        result = agent.run(self.task, snapshot, context())
        caps = agent.capabilities()
        builder = PublicFeedbackBuilder(ContextPolicy(output_fields=["marker"],
            state_fields=caps.feedback_state_fields, tool_arguments=caps.feedback_tool_arguments,
            tool_outputs=caps.feedback_tool_outputs))
        evaluation = EvaluationResult(evaluation_id="eval", task_id=result.task_id, agent_version_id=result.agent_version_id,
            execution_operation_id=result.operation_id, operation_id="eval", score=1.0, success=True, metrics={}, feedback="private")
        _, public, _ = builder.public_views(self.task, result, evaluation)
        self.assertEqual(public.state_summary["read_skills"], ("test-skill",))
        output = next(event.payload["output"] for event in public.trajectory if event.kind == "tool_result")
        self.assertIn("VERSION_A", output["content"])
        self.assertNotIn("private", public.model_dump_json())

    def test_import_freezes_content_and_restores_without_source_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            skill_dir = root / "skills/test-skill"
            skill_dir.mkdir(parents=True)
            source = skill_dir / "SKILL.md"
            source.write_bytes(library().skills[0].content.encode("utf-8"))
            config = yaml.safe_load((ROOT / "experiments/configs/v0_1_no_evolution.yaml").read_text(encoding="utf-8"))
            config["storage"]["root"] = "runs"
            config["agent"]["config"] = {"skills": {"initial_library": "skills"}}
            path = root / "experiment.yaml"
            path.write_text(yaml.safe_dump(config), encoding="utf-8")
            initial = prepare_storage(path, run_id="first")
            snapshot = initial.version_store.stable()
            source.write_text(library("CHANGED_SOURCE").skills[0].content, encoding="utf-8")
            runtime = SkillRuntime.from_snapshot(snapshot, initial.artifact_store, "exec")
            self.assertIn("VERSION_A", runtime.read_skill("test-skill", call_id="1")["content"])
            ref = initial.version_store.save(snapshot)
            config["agent"]["initial_snapshot_ref"] = str(initial.run_root / ref.uri)
            config["agent"]["initial_prompt"] = None
            source.unlink()
            skill_dir.rmdir()
            skill_dir.parent.rmdir()
            path.write_text(yaml.safe_dump(config), encoding="utf-8")
            restored = prepare_storage(path, run_id="restored")
            self.assertEqual(snapshot, restored.version_store.stable())
            self.assertEqual(decode_library(restored.artifact_store.get(snapshot.artifacts["skill"])), library())

    def test_import_rejects_attachments_duplicate_names_and_inconsistent_metadata(self):
        with self.assertRaises(ValueError):
            SkillLibrary(skills=[library().skills[0], library().skills[0]])
        with self.assertRaises(ValueError):
            parse_skill("---\nname: a\nname: b\ndescription: d\n---\nbody")
        invalid = library().model_dump(mode="json")
        invalid["skills"][0]["description"] = "Contradicts the source"
        with self.assertRaises(ValueError):
            decode_library(json.dumps(invalid))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            child = root / "test-skill"
            child.mkdir()
            (child / "SKILL.md").write_text(library().skills[0].content, encoding="utf-8")
            (child / "helper.py").write_text("pass", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "attachments"):
                import_library(root)

    def test_invalid_or_foreign_service_reports_do_not_replace_receipts(self):
        _, snapshot, store = fixture(reading_model())
        runtime = SkillRuntime.from_snapshot(snapshot, store, "report-probe")
        session = CodexSkillSession(runtime)
        try:
            foreign = SkillRuntime.from_snapshot(snapshot, store, "another-execution")
            foreign.read_skill("test-skill", call_id="1")
            for report in (None, {"reads": []}, {"status": "ready", "discovered": True, "reads": [None]},
                           {"status": "ready", "discovered": True, "reads": foreign.reads}):
                with self.subTest(report=report):
                    session.report_path.write_text(json.dumps(report), encoding="utf-8")
                    with self.assertRaises(ValueError):
                        session.report()
                    self.assertEqual(runtime.reads, [])
        finally:
            session.close()

    def test_mcp_and_direct_transport_return_identical_content_and_clean_up(self):
        _, snapshot, store = fixture(reading_model())
        runtime = SkillRuntime.from_snapshot(snapshot, store, "mcp-probe")
        session = CodexSkillSession(runtime)
        root = session.report_path.parent
        async def exercise():
            import tomllib
            settings = tomllib.loads(session.overrides[0])["mcp_servers"]["aer_skills"]
            async with stdio_client(StdioServerParameters(command=settings["command"], args=settings["args"])) as streams:
                async with ClientSession(*streams) as client:
                    await client.initialize()
                    self.assertEqual([tool.name for tool in (await client.list_tools()).tools], ["read_skill"])
                    result = await client.call_tool("read_skill", {"name": "test-skill"})
                    self.assertFalse(result.isError, result)
                    self.assertEqual(result.structuredContent["content"], library().skills[0].content)
                    missing = await client.call_tool("read_skill", {"name": "absent"})
                    self.assertTrue(missing.isError)
                    invalid = await client.call_tool("read_skill", {"name": "test-skill", "path": "../other"})
                    self.assertTrue(invalid.isError)
        try:
            asyncio.run(exercise())
            self.assertTrue(session.report()["discovered"])
            self.assertEqual(runtime.summary("mcp")["read_skills"], ["test-skill"])
        finally:
            session.close()
        self.assertFalse(root.exists())
