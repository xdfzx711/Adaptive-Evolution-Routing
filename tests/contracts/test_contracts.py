import importlib
import inspect
import subprocess
import sys
import unittest
from pathlib import Path
from typing import get_type_hints

from aer_core.contracts.agent import AgentPlugin
from aer_core.contracts.evolution import EvolutionPlugin
from aer_core.contracts.router import RouterPlugin
from aer_core.errors import (
    AERError, BudgetExceeded, CommitError, ContractError, EvaluationError,
    EvolutionError, ExecutionError, OperationTimeout, PluginError,
    RoutingError, StorageError, UnsupportedCapability, ValidationError,
)
from aer_core.schemas.evolution import (
    AgentCapabilities, EvolutionCandidate, EvolutionCapabilities,
    EvolutionDecision, EvolutionRequest, EvolutionResponse,
)
from aer_core.schemas.execution import ErrorInfo, ExecutionView
from aer_core.schemas.usage import ResourceUsage
from tests.domain_helpers import artifact, context, execution, run_context, snapshot, task_view


class EchoAgent:
    def __init__(self):
        self.current = snapshot()

    def capabilities(self):
        return AgentCapabilities(supported_targets=["prompt"], supported_artifact_kinds=["prompt"], can_rebuild_session=True, snapshot_scope="controlled_config")

    def run(self, task, stable, run_ctx):
        return execution(stable.version_id).replace(task_id=task.task_id, operation_id=run_ctx.operation_id, output={"answer": "echo"})

    def snapshot(self):
        return self.current

    def restore(self, stable):
        self.current = stable


class AlternateAgent(EchoAgent):
    def run(self, task, stable, run_ctx):
        result = super().run(task, stable, run_ctx)
        return result.replace(output={"answer": "alternate"})


class FixedDecision:
    def select(self, public_context):
        return EvolutionDecision(target=public_context.feasible_targets[0], rationale="fixed")


class ManualDecision:
    def select(self, public_context):
        return EvolutionDecision(target="prompt", rationale="manual")


class CandidateGenerator:
    candidate_id = "candidate-a"

    def capabilities(self):
        return EvolutionCapabilities(supported_targets=["prompt"], compatible_agents=["mock_agent"], required_artifact_kinds=["prompt"], supports_multiple_candidates=False, reports_usage=True)

    def evolve(self, request):
        candidate = EvolutionCandidate(candidate_id=self.candidate_id, parent_version_id=request.context.stable_snapshot.version_id, target=request.decision.target, artifact_updates={"prompt": artifact(version=self.candidate_id)})
        return EvolutionResponse(candidates=[candidate], usage=ResourceUsage.zero(), status="completed")


class AlternateGenerator(CandidateGenerator):
    candidate_id = "candidate-b"


class ContractTests(unittest.TestCase):
    def test_two_agents_routers_and_evolvers_share_the_same_contract_calls(self):
        for agent in [EchoAgent(), AlternateAgent()]:
            self.assertIsInstance(agent, AgentPlugin)
            stable = agent.snapshot()
            before = stable.model_dump_json()
            for router in [FixedDecision(), ManualDecision()]:
                self.assertIsInstance(router, RouterPlugin)
                for evolver in [CandidateGenerator(), AlternateGenerator()]:
                    with self.subTest(agent=type(agent).__name__, router=type(router).__name__, evolver=type(evolver).__name__):
                        self.assertIsInstance(evolver, EvolutionPlugin)
                        live = run_context()
                        result = agent.run(task_view(), stable, live)
                        public_result = ExecutionView(**{
                            key: result.model_dump()[key]
                            for key in ExecutionView.model_fields
                        })
                        public_context = context().replace(execution=public_result)
                        decision = router.select(public_context)
                        request = EvolutionRequest(attempt_id="attempt-1", round_id=1, context=public_context, decision=decision, operator_id=type(evolver).__name__, max_candidates=1, run_context=live)
                        response = evolver.evolve(request)
                        self.assertEqual(response.candidates[0].parent_version_id, stable.version_id)
                        self.assertEqual(EvolutionResponse.model_validate_json(response.model_dump_json()), response)
                        self.assertEqual(stable.model_dump_json(), before)

    def test_all_protocol_signatures_have_resolvable_types_and_are_synchronous(self):
        root = Path(__file__).resolve().parents[2] / "aer_core/contracts"
        for path in root.glob("*.py"):
            module = importlib.import_module("aer_core.contracts." + path.stem)
            for item in vars(module).values():
                if isinstance(item, type) and getattr(item, "_is_protocol", False) and item.__module__ == module.__name__:
                    for name, method in vars(item).items():
                        if not name.startswith("_") and inspect.isfunction(method):
                            with self.subTest(protocol=item.__name__, method=name):
                                self.assertFalse(inspect.iscoroutinefunction(method))
                                hints = get_type_hints(method)
                                self.assertIn("return", hints)
                                for parameter in inspect.signature(method).parameters.values():
                                    if parameter.name != "self":
                                        self.assertIn(parameter.name, hints)

    def test_contract_imports_do_not_load_plugins_or_agent_model_sdks(self):
        script = """
import importlib, pkgutil, sys
import aer_core.contracts
for item in pkgutil.iter_modules(aer_core.contracts.__path__):
    importlib.import_module('aer_core.contracts.' + item.name)
for name in ('plugins', 'openai', 'anthropic', 'langchain', 'transformers'):
    assert not any(key == name or key.startswith(name + '.') for key in sys.modules), name
"""
        result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_runtime_errors_keep_stage_identity_and_usage_in_serializable_info(self):
        classes = [AERError, PluginError, UnsupportedCapability, ContractError, ExecutionError, EvaluationError, RoutingError, EvolutionError, ValidationError, CommitError, StorageError, OperationTimeout, BudgetExceeded]
        for error_type in classes:
            with self.subTest(error=error_type.__name__):
                error = error_type("operation failed", stage="evolution", operation_id="op-1", plugin_id="plugin-1", usage=ResourceUsage.zero())
                self.assertIsInstance(error, AERError)
                info = error.to_info()
                self.assertEqual(info.code, error_type.code)
                self.assertEqual(info.operation_id, "op-1")
                self.assertEqual(info.usage.total_tokens, 0)
                self.assertEqual(ErrorInfo.model_validate_json(info.model_dump_json()), info)

    def test_budget_failure_is_distinct_from_plugin_failure(self):
        error = BudgetExceeded("no tokens remaining", stage="evolution")
        self.assertNotIsInstance(error, PluginError)
        self.assertEqual(error.to_info().code, "budget_exceeded")
