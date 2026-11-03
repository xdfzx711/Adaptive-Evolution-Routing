import unittest

from aer_core.config.models import RoutingSettings
from aer_core.errors import ManualPlanExhausted, RoutingError
from aer_core.schemas.evolution import EvolutionCapabilities
from aer_core.targets import OperatorBinding, OperatorResolver, TargetRegistry
from plugins.evolution.prompt import PromptEvolverConfig, SimplePromptEvolver
from plugins.providers.scripted import ScriptedModel
from plugins.routers.basic import FixedRouter, ManualRouter
from tests.evolution.helpers import evolution_context, fixture
from tests.execution.helpers import MODEL


class RoutingTests(unittest.TestCase):
    def test_fixed_router_and_configured_operator_resolution(self):
        state, artifacts, _ = evolution_context()
        router = FixedRouter(RoutingSettings(plugin="fixed_router", fixed_target="prompt", manual_decisions=[]))
        self.assertEqual(router.select(state).target, "prompt")
        operator = SimplePromptEvolver(PromptEvolverConfig(max_candidates=1), artifacts, ScriptedModel(MODEL))
        resolver = OperatorResolver({"prompt": "simple_prompt_evolver"}, {"prompt": OperatorBinding("simple_prompt_evolver", operator)})
        self.assertIs(resolver.resolve("prompt").operator, operator)
        with self.assertRaises(RoutingError):
            resolver.resolve("memory")

    def test_fixed_router_cannot_choose_infeasible_target(self):
        state, _, _ = evolution_context()
        router = FixedRouter(RoutingSettings(plugin="fixed_router", fixed_target="memory", manual_decisions=[]))
        with self.assertRaises(RoutingError):
            router.select(state)

    def test_manual_router_uses_explicit_round_and_stops_at_plan_end(self):
        config = RoutingSettings(plugin="manual_router", fixed_target=None, manual_decisions=[
            {"round_id": 1, "target": "prompt", "rationale": "first"},
            {"round_id": 2, "target": "prompt", "rationale": "second"}])
        router = ManualRouter(config)
        state, _, _ = evolution_context(round_id=2)
        self.assertEqual(router.select(state).rationale, "second")
        with self.assertRaises(ManualPlanExhausted):
            router.select(state.replace(round_id=3))

    def test_manual_invalid_target_and_noncontinuous_plan_are_rejected(self):
        state, _, _ = evolution_context()
        router = ManualRouter(RoutingSettings(plugin="manual_router", fixed_target=None,
            manual_decisions=[{"round_id": 1, "target": "memory", "rationale": "manual"}]))
        with self.assertRaises(RoutingError):
            router.select(state)
        with self.assertRaises(ValueError):
            ManualRouter(RoutingSettings(plugin="manual_router", fixed_target=None,
                manual_decisions=[{"round_id": 2, "target": "prompt", "rationale": "gap"}]))

    def test_target_intersection_requires_agent_operator_and_artifacts(self):
        for single in (False, True):
            agent, stable, artifacts, *_ = fixture(single)
            operator = SimplePromptEvolver(PromptEvolverConfig(max_candidates=1), artifacts, ScriptedModel(MODEL))
            resolver = OperatorResolver({"prompt": "simple_prompt_evolver"}, {"prompt": OperatorBinding("simple_prompt_evolver", operator)})
            registry = TargetRegistry(resolver)
            available = registry.feasible(agent, stable, ["prompt", "memory", "model"])
            self.assertEqual(available.targets, ("prompt",))
            self.assertEqual(available.availability[1].reason_code, "operator_unavailable")
            self.assertEqual(available.availability[2].reason_code, "agent_target_unsupported")
            self.assertEqual(registry.feasible(agent, stable.replace(artifacts={}), ["prompt"]).targets, ())
            self.assertEqual(TargetRegistry(OperatorResolver({"prompt": "missing"}, {})).feasible(agent, stable, ["prompt"]).targets, ())

    def test_operator_mismatch_agent_compatibility_and_session_rebuild_are_checked(self):
        agent, stable, _, *_ = fixture()
        class IncompatibleOperator:
            def capabilities(self):
                return EvolutionCapabilities(supported_targets=["prompt"], compatible_agents=["single_call_agent"],
                    required_artifact_kinds=["prompt"], supports_multiple_candidates=False, reports_usage=True)
        resolver = OperatorResolver({"prompt": "selected"}, {"prompt": OperatorBinding("selected", IncompatibleOperator())})
        self.assertEqual(TargetRegistry(resolver).feasible(agent, stable, ["prompt"]).availability[0].reason_code, "agent_operator_incompatible")
        caps = agent.capabilities()
        agent.capabilities = lambda: caps.replace(can_rebuild_session=False)
        self.assertEqual(TargetRegistry(resolver).feasible(agent, stable, ["prompt"]).availability[0].reason_code, "session_rebuild_unsupported")
        with self.assertRaises(RoutingError):
            OperatorResolver({"prompt": "different"}, resolver.operators).resolve("prompt")
