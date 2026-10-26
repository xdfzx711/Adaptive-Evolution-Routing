"""Focused checks for interface/disclosure composition, without external models."""

import json
import tempfile
import unittest
from contextlib import ExitStack
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import yaml

from aer_core.config.models import AgentSettings, Settings, ToolProviderSettings
from aer_core.config.partitions import BenchmarkIndex, IndexTask
from aer_core.errors import ContractError, EvaluationError
from aer_core.feedback import PublicFeedbackBuilder
from aer_core.schemas.evolution import AgentCapabilities, EvolutionCapabilities, EvolutionCandidate, EvolutionResponse
from aer_core.schemas.execution import Diagnosis, EvaluationResult, ExecutionResult, TraceEvent
from aer_core.schemas.feedback import ContextPolicy, TaskFeedbackPolicy
from aer_core.schemas.plugins import PluginCapabilities, PluginMetadata
from aer_core.schemas.providers import ModelMessage, ModelRequest, ToolCall, ToolResult, ToolSpec
from aer_core.schemas.task import EvaluationSpec, Task, TaskView
from aer_core.schemas.usage import ResourceUsage, UsageReport
from experiments.bootstrap import create_registry
from experiments.execution import prepare_execution
from experiments.orchestration import prepare_orchestrator
from tests.execution.helpers import RecordingModel, response
from tests.orchestration.helpers import ROOT


PRIVATE = "PRIVATE_FEEDBACK_CONTRACT_CANARY"


def model_usage():
    return ResourceUsage(input_tokens=3, output_tokens=2, total_tokens=5, model_calls=1, tool_calls=0,
                         elapsed_seconds=0, usage_complete=True, usage_sources=["simulated"])


class Empty(Settings):
    pass


class NativeBenchmark:
    dataset_version = "native.v1"
    source_paths = ()

    def __init__(self):
        self.entries = tuple(Task(task_id=name, benchmark_id="native", input={"text": text},
            metadata={"validation_group": group} if group else {}) for name, text, group in
            (("dev", "alpha", None), ("similar", "beta", "similar"),
             ("regression", "gamma", "regression"), ("test", "delta", None)))

    def tasks(self, split):
        return {"dev": self.entries[:1], "validation": self.entries[1:3], "test": self.entries[3:]}[split]

    def task_view(self, task):
        return TaskView(task_id=task.task_id, benchmark_id="native", input=dict(task.input),
                        public_metadata={"topic": "case conversion", "private": PRIVATE})

    def evaluation_spec(self, task):
        return EvaluationSpec(task=task.identity, scoring_method="native", private_parameters={
            "expected": task.input["text"].upper(), "label": PRIVATE})

    def feedback_policy(self, task):
        return TaskFeedbackPolicy(task=task.identity, output_fields=["normalized"], metadata_fields=["topic"],
            allow_evolution=task in self.tasks("dev"), reason_messages={"needs_revision": "Public output did not pass."})

    def index(self):
        return BenchmarkIndex(benchmark_id="native", splits={split: [IndexTask(task_id=task.task_id,
            source_id=task.task_id, metadata=dict(task.metadata)) for task in self.tasks(split)]
            for split in ("dev", "validation", "test")})


class NativeEvaluator:
    scorer_version = "native.score.v1"
    score_policy = "exact normalized text"

    def evaluate(self, task, result, spec, run_ctx):
        passed = result.output.get("normalized") == spec.private_parameters["expected"]
        return EvaluationResult(evaluation_id=run_ctx.operation_id + "/result", task_id=task.task_id,
            agent_version_id=result.agent_version_id, execution_operation_id=result.operation_id,
            operation_id=run_ctx.operation_id, score=float(passed), success=passed, metrics={},
            feedback=PRIVATE, reason_code="correct" if passed else "needs_revision")


class NativeTools:
    def specifications(self):
        return (ToolSpec(name="uppercase", description="Uppercase text", parameters={"type": "object",
            "properties": {"text": {"type": "string"}, "credential": {"type": "string"}}},
            feedback_arguments=["text"], feedback_outputs=["upper"]),)

    def invoke(self, call, run_ctx):
        return ToolResult(call_id=call.call_id, name=call.name,
            output={"upper": call.arguments["text"].upper(), "private": PRIVATE}, status="completed",
            usage=UsageReport(input_tokens=0, output_tokens=0, model_calls=0, tool_calls=1,
                              elapsed_seconds=0, usage_source="simulated"))


class NativeAgent:
    def __init__(self, initial, artifacts, tools):
        self.initial, self.artifacts, self.tools = initial, artifacts, tools

    def capabilities(self):
        return AgentCapabilities(supported_targets=["prompt"], supported_artifact_kinds=["prompt"],
            can_rebuild_session=True, snapshot_scope="native", feedback_state_fields=["visits"],
            feedback_config_fields=["strategy"])

    def snapshot(self):
        return self.initial

    def restore(self, snapshot):
        self.initial = snapshot

    def run(self, task, snapshot, run_ctx):
        call = ToolCall(call_id="convert", name="uppercase", arguments={"text": task.input["text"], "credential": PRIVATE})
        observation = self.tools.invoke(call, run_ctx)
        improved = "AMEND" in self.artifacts.get(snapshot.artifacts["prompt"]).decode()
        output = {"normalized": observation.output["upper"] if improved else task.input["text"], "private": PRIVATE}
        return ExecutionResult(task_id=task.task_id, agent_version_id=snapshot.version_id, operation_id=run_ctx.operation_id,
            output=output, trajectory=[TraceEvent(seq=1, kind="tool_call", payload=call.model_dump(mode="json")),
                TraceEvent(seq=2, kind="tool_result", payload=observation.model_dump(mode="json")),
                TraceEvent(seq=3, kind="finish", payload={"output": output})],
            state_summary={"visits": 1, "private": PRIVATE},
            usage=ResourceUsage.zero().replace(tool_calls=1), status="completed")


class RenamedDiagnoser:
    def __init__(self, model):
        self.model = model

    def diagnose(self, task, result, feedback, run_ctx):
        self.model.generate(ModelRequest(messages=[ModelMessage(role="user", content=json.dumps({
            "task": task.model_dump(mode="json"), "execution": result.model_dump(mode="json"),
            "feedback": feedback.model_dump(mode="json")}))], seed=run_ctx.seed, max_output_tokens=64), run_ctx)
        return Diagnosis(reason_code="needs_revision", explanation="Public output needs revision.",
                         evidence_step_ids=[3], source="model", usage=model_usage())


class NativeEvolver:
    def __init__(self, model, artifacts):
        self.model, self.artifacts = model, artifacts

    def capabilities(self):
        return EvolutionCapabilities(supported_targets=["prompt"], compatible_agents=["native_agent"],
            required_artifact_kinds=["prompt"], supports_multiple_candidates=False, reports_usage=True)

    def evolve(self, request):
        self.model.generate(ModelRequest(messages=[ModelMessage(role="user", content=request.context.model_dump_json())],
            seed=request.run_context.seed, max_output_tokens=64), request.run_context)
        prompt = self.artifacts.put(b"AMEND: normalize text to uppercase", "prompt")
        candidate = EvolutionCandidate(candidate_id=request.attempt_id + "/candidate/1",
            parent_version_id=request.context.stable_snapshot.version_id, target="prompt", artifact_updates={"prompt": prompt})
        return EvolutionResponse(candidates=[candidate], usage=model_usage(), status="completed")


def native_registry(model):
    registry = create_registry()
    def register(name, category, factory, schema=Empty, deps=None, caps=None, **kwargs):
        deps = deps or {}
        registry.register(PluginMetadata(id=name, category=category, api_version="1.0", plugin_version="0.1.0",
            dependencies=list(deps), capabilities=caps or PluginCapabilities()), factory,
            config_model=schema, dependency_types=deps, **kwargs)
    from aer_core.contracts.storage import ArtifactStore
    from aer_core.contracts.providers import ModelProvider, ToolProvider
    from aer_core.schemas.references import AgentSnapshot
    register("native_benchmark", "benchmark", lambda **kw: NativeBenchmark(), caps=PluginCapabilities(
        benchmark_id="native", splits=["dev", "validation", "test"]))
    register("native_evaluator", "evaluator", lambda **kw: NativeEvaluator())
    register("native_model", "model_provider", lambda **kw: model)
    register("native_tools", "tool_provider", lambda **kw: NativeTools(), ToolProviderSettings,
             caps=PluginCapabilities(tools=["uppercase"]))
    register("native_agent", "agent", lambda *, config, dependencies: NativeAgent(dependencies.get("initial_snapshot"),
        dependencies.get("artifact_store"), dependencies.get("tool_provider")), AgentSettings,
        {"initial_snapshot": AgentSnapshot, "artifact_store": ArtifactStore, "tool_provider": ToolProvider},
        PluginCapabilities(targets=["prompt"]), snapshot_config=lambda config: {"strategy": "uppercase"})
    register("renamed_diagnoser", "diagnoser", lambda *, config, dependencies: RenamedDiagnoser(dependencies.get("model_provider")),
             deps={"model_provider": ModelProvider})
    from plugins.mocks import MockEvolverConfig
    register("native_evolver", "evolution", lambda *, config, dependencies: NativeEvolver(
        dependencies.get("model_provider"), dependencies.get("artifact_store")), MockEvolverConfig,
        {"model_provider": ModelProvider, "artifact_store": ArtifactStore},
        PluginCapabilities(targets=["prompt"], compatible_agents=["native_agent"]))
    return registry


class FeedbackContractTests(unittest.TestCase):
    def setup_native(self, directory, stack, *, weighted=False):
        model = RecordingModel(lambda *_: response("{}"))
        registry = native_registry(model)
        for module in ("configuration", "benchmark", "execution", "storage", "evolution", "orchestration", "validation"):
            stack.enter_context(patch(f"experiments.{module}.create_registry", return_value=registry))
        config = yaml.safe_load((ROOT / "experiments/configs/v0_1_fixed_prompt.yaml").read_text(encoding="utf-8"))
        config["storage"]["root"] = str(directory / "runs")
        config["benchmark"]["plugin"] = "native_benchmark"
        config["agent"]["plugin"] = "native_agent"
        config["assessment"] = {"evaluator": "native_evaluator", "diagnoser": "renamed_diagnoser"}
        config["providers"] = {"model": {"plugin": "native_model", "model_id": "fixture", "config": {}},
                               "tools": {"plugin": "native_tools", "enabled": ["uppercase"]}}
        config["targets"]["default_operators"]["prompt"] = "native_evolver"
        config["limits"]["max_evolution_rounds"] = 1
        if weighted:
            config["validation"].update(acceptance_policy="weighted_improvement", max_regression_drop=None,
                weights={"current_task": 0.5, "similar": 0.25, "regression": 0.25})
            config["limits"]["max_evolution_rounds"] = 2
        path = directory / "native.yaml"
        path.write_text(yaml.safe_dump(config), encoding="utf-8")
        return path, model

    def test_native_input_tool_and_snapshot_use_same_public_views_in_both_entrypoints(self):
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            path, model = self.setup_native(Path(directory), stack)
            prepared = prepare_execution(path, run_id="standalone")
            self.assertFalse(hasattr(prepared.benchmark.evaluator, "feedback_view"))
            self.assertFalse(hasattr(prepared.benchmark.evaluator, "evaluate_record"))
            record = prepared.run_task("dev")
            self.assertIsNone(record.assessment_error)
            first_input = json.loads(model.requests[-1][0].messages[-1].content)
            self.assertNotIn(PRIVATE, json.dumps(first_input))
            self.assertEqual(first_input["execution"]["state_summary"], {"visits": 1})
            self.assertEqual(first_input["execution"]["trajectory"][0]["payload"]["arguments"], {"text": "alpha"})
            self.assertEqual(first_input["execution"]["trajectory"][1]["payload"]["output"], {"upper": "ALPHA"})
            self.assertEqual(record.feedback.reason_code, "needs_revision")
            scored = prepared.benchmark.score(record.execution, operation_id="offline-native")
            self.assertEqual(prepared.benchmark.public_feedback(scored), record.feedback)
            self.assertEqual(prepared.benchmark.as_dict()["score_policy"], "exact normalized text")
            startup = prepare_orchestrator(path, run_id="controller")
            result = startup.run_task("dev")
            self.assertEqual(result.stop_reason, "task_succeeded")
            self.assertEqual(result.final_execution.output["normalized"], "ALPHA")
            self.assertEqual(startup.execution.storage.version_store.load(result.initial_version_id).config, {"strategy": "uppercase"})
            second_input = json.loads(model.requests[-2][0].messages[-1].content)
            self.assertEqual(second_input["feedback"], first_input["feedback"])
            self.assertEqual(second_input["execution"]["output"], first_input["execution"]["output"])
            evolution_input = json.loads(model.requests[-1][0].messages[-1].content)
            self.assertEqual(evolution_input["stable_snapshot"]["config"], {"strategy": "uppercase"})
            for request, _ in model.requests:
                self.assertNotIn(PRIVATE, request.model_dump_json())

    def test_providers_are_built_for_consumers_other_than_agent(self):
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            path, model = self.setup_native(Path(directory), stack)
            prepared = prepare_execution(path)
            self.assertFalse(hasattr(prepared.agent, "model"))
            self.assertIs(prepared.diagnoser.model, model)
            self.assertIs(prepared.built_plugins.instances["model"], model)
            # The Evolver can be the sole model consumer as well.
            config = yaml.safe_load(path.read_text(encoding="utf-8"))
            config["assessment"]["diagnoser"] = "lightweight_diagnoser"
            path.write_text(yaml.safe_dump(config), encoding="utf-8")
            prepared = prepare_execution(path)
            self.assertIs(prepared.built_plugins.instances["model"], model)

    def test_evaluator_usage_is_charged_once_on_success_contract_failure_and_exception(self):
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            path, _ = self.setup_native(Path(directory), stack)
            startup = prepare_orchestrator(path)
            task = startup.prepare_task("dev")
            evaluator = task.execution.benchmark.evaluator
            original = evaluator.evaluate
            usage = model_usage()
            for incorrect_identity in (False, True):
                with patch.object(evaluator, "evaluate", side_effect=lambda *args, bad=incorrect_identity:
                    original(*args).replace(usage=usage, operation_id="foreign" if bad else args[3].operation_id)):
                    scored = task.controller.executor.execute(task.task, task.execution.storage.version_store.stable(),
                                                              replace(task.context, seed=100 + int(incorrect_identity)))
                    self.assertEqual(scored.usage.total_tokens, 5)
                    self.assertEqual(scored.usage.model_calls, 1)
                    self.assertEqual(scored.usage.tool_calls, 1)
                    self.assertEqual(scored.error is not None, incorrect_identity)
            with patch.object(evaluator, "evaluate", side_effect=EvaluationError("fixture", stage="evaluation", usage=usage)):
                scored = task.controller.executor.execute(task.task, task.execution.storage.version_store.stable(),
                                                          replace(task.context, seed=102))
                self.assertEqual(scored.usage.total_tokens, 5)
                self.assertIsNone(scored.evaluation)

    def test_feedback_requires_explicit_task_permission_and_matching_evidence(self):
        from tests.domain_helpers import execution, evaluation, task_view
        result = execution()
        public = task_view()
        builder = PublicFeedbackBuilder(ContextPolicy(output_fields=["answer"]))
        with self.assertRaises(ContractError):
            builder.public_views(public, result, evaluation(result).replace(agent_version_id="foreign"))
        from aer_core.context import ContextBuilder
        from tests.evolution.helpers import fixture, MemoryExperience
        _, stable, _, public, record, live, policy = fixture()
        # Metadata is not authorization: the policy is supplied by the Benchmark.
        builder = ContextBuilder(MemoryExperience(), policy)
        state = builder.build(public.replace(public_metadata={}), record, stable, live, round_id=1, feasible_targets=["prompt"])
        self.assertEqual(state.task.public_metadata, {})
        with self.assertRaises(ContractError):
            ContextBuilder(MemoryExperience(), policy.replace(allow_evolution=False)).build(
                public, record, stable, live, round_id=1, feasible_targets=["prompt"])

    def test_continuous_scores_keep_score_success_and_candidate_acceptance_distinct(self):
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            path, _ = self.setup_native(Path(directory), stack)
            startup = prepare_orchestrator(path)
            task = startup.prepare_task("dev")
            evaluator = task.execution.benchmark.evaluator
            original = evaluator.evaluate
            def partial_credit(*args):
                result = original(*args)
                return result.replace(score=0.8 if result.success else 0.3, success=False, reason_code="needs_revision")
            with patch.object(evaluator, "evaluate", side_effect=partial_credit):
                result = task.run()
            self.assertIsNone(result.error)
            self.assertEqual(result.stop_reason, "max_evolution_rounds_reached")
            self.assertEqual(result.final_evaluation.score, 0.8)
            self.assertFalse(result.final_evaluation.success)
            self.assertNotEqual(result.initial_version_id, result.final_stable_version_id)
            events = task.execution.storage.experience_store.events()
            committed = [event.payload for event in events if event.record_type == "candidate_evaluated" and event.payload.committed]
            self.assertEqual(len(committed), 1)
            self.assertEqual(committed[0].acceptance.delta_score, 0.5)

    def test_weighted_commit_history_and_success_condition_are_independent(self):
        for solved in (False, True):
            with self.subTest(solved=solved), tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
                path, model = self.setup_native(Path(directory), stack, weighted=True)
                startup = prepare_orchestrator(path)
                task = startup.prepare_task("dev")
                initial_id = task.execution.storage.version_store.stable().version_id
                evaluator = task.execution.benchmark.evaluator
                original = evaluator.evaluate

                def tradeoff(public, execution, spec, run_ctx):
                    result = original(public, execution, spec, run_ctx)
                    changed = execution.agent_version_id != initial_id
                    score = ((1.0 if solved else 0.52) if changed else 0.0) if public.task_id == "dev" else (
                        (0.3 if public.task_id == "similar" else 0.2) if changed else 0.4)
                    return result.replace(score=score, success=bool(solved and public.task_id == "dev" and changed),
                                          reason_code="correct" if solved and public.task_id == "dev" and changed else "needs_revision")

                evaluator.evaluate = tradeoff
                result = task.run()
                self.assertIsNone(result.error)
                self.assertNotEqual(result.final_stable_version_id, initial_id)
                self.assertEqual(result.stop_reason, "task_succeeded" if solved else "max_evolution_rounds_reached")
                self.assertEqual(result.evolution_rounds_used, 1 if solved else 2)
                self.assertEqual(result.final_evaluation.success, solved)
                events = task.execution.storage.experience_store.events()
                committed = [event.payload for event in events if event.record_type == "candidate_evaluated" and event.payload.committed]
                self.assertEqual(len(committed), 1)
                decision = committed[0].acceptance
                self.assertLess(decision.delta_score, 0)
                self.assertAlmostEqual(decision.reward, 0.425 if solved else 0.185)
                root = task.execution.storage.run_root
                text = (root / "evolution.txt").read_text(encoding="utf-8")
                self.assertIn("回归硬限制关闭", text)
                self.assertIn("当前任务贡献", text)
                self.assertIn("综合收益", text)
                self.assertNotIn("报告异常", text)
                records = sorted(root.glob("rounds/*/round-*/record.json"))
                first = json.loads(records[0].read_text(encoding="utf-8"))
                self.assertEqual(first["candidates"][0]["acceptance"]["weighted_gain"]["total"], decision.reward)
                if not solved:
                    second = json.loads(records[1].read_text(encoding="utf-8"))
                    history = second["input_to_evolver"]["history"][0]["candidates"][0]
                    self.assertEqual(history["weighted_gain"]["total"], decision.reward)
                    self.assertEqual(history["policy_id"], "weighted_improvement")
                    evolver_inputs = [json.loads(request.messages[-1].content) for request, _ in model.requests
                                      if '"stable_snapshot"' in request.messages[-1].content]
                    self.assertEqual(evolver_inputs[-1]["history"][0]["candidate_summaries"][0]["reward"], decision.reward)
                    self.assertNotIn(PRIVATE, json.dumps(evolver_inputs[-1]))

    def test_model_evaluator_shares_provider_and_charges_once_through_validation_and_reuse(self):
        class ModelEvaluator(NativeEvaluator):
            def __init__(self, model):
                self.model = model

            def evaluate(self, task, result, spec, run_ctx):
                self.model.generate(ModelRequest(messages=[ModelMessage(role="user", content="Evaluate fixture")],
                    seed=run_ctx.seed, max_output_tokens=64), run_ctx)
                return super().evaluate(task, result, spec, run_ctx).replace(usage=model_usage())

        from aer_core.contracts.providers import ModelProvider
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            path, model = self.setup_native(Path(directory), stack)
            from experiments import benchmark
            registry = benchmark.create_registry()
            registry.register(PluginMetadata(id="model_evaluator", category="evaluator", api_version="1.0",
                plugin_version="0.1.0", dependencies=["model_provider"]),
                lambda *, config, dependencies: ModelEvaluator(dependencies.get("model_provider")),
                config_model=Empty, dependency_types={"model_provider": ModelProvider})
            config = yaml.safe_load(path.read_text(encoding="utf-8"))
            config["assessment"]["evaluator"] = "model_evaluator"
            path.write_text(yaml.safe_dump(config), encoding="utf-8")
            startup = prepare_orchestrator(path)
            task = startup.prepare_task("dev")
            self.assertIs(task.execution.benchmark.evaluator.model, task.execution.diagnoser.model)
            self.assertIs(task.execution.built_plugins.instances["model"], task.execution.diagnoser.model)
            result = task.run()
            self.assertEqual(result.stop_reason, "task_succeeded")
            self.assertEqual(result.usage.model_calls, len(model.requests))
            self.assertEqual(result.usage.total_tokens, 5 * len(model.requests))
            self.assertEqual(len([ctx for _, ctx in model.requests if ctx.stage == "evaluation"]), 6)


if __name__ == "__main__":
    unittest.main()
