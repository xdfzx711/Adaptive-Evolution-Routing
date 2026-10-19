"""Registry and Manager integration without a tests/plugins package collision."""

import tempfile
import unittest
from pathlib import Path

import yaml

from aer_core.config.models import Settings
from aer_core.contracts.router import RouterPlugin
from aer_core.contracts.storage import ArtifactStore
from aer_core.errors import (
    ContractError, DependencyError, DuplicatePluginError,
    PluginConfigurationError, PluginInstantiationError,
    PluginNotFound, PluginRegistrationError, PluginVersionError,
)
from aer_core.plugin_manager import PluginManager
from aer_core.plugin_registry import API_VERSION, PluginRegistry
from aer_core.schemas.evolution import EvolutionContext, EvolutionDecision, EvolutionRequest
from aer_core.schemas.execution import ExecutionView, FeedbackView
from aer_core.schemas.plugins import AssemblyManifest, PluginBinding, PluginCapabilities, PluginMetadata
from aer_core.schemas.references import AgentSnapshot
from experiments.bootstrap import assemble_experiment, create_registry
from experiments.configuration import load_experiment_config
from plugins.mocks import MockArtifactStore, register_mock_plugins
from tests.domain_helpers import NOW, context, remaining, run_context, task_view


ROOT = Path(__file__).resolve().parents[2]
CONFIGS = ROOT / "experiments/configs"


class EmptySettings(Settings):
    pass


class TestRouter:
    def select(self, context: EvolutionContext) -> EvolutionDecision:
        return EvolutionDecision(target=context.feasible_targets[0], rationale="test")


def router_factory(*, config, dependencies):
    return TestRouter()


def metadata(plugin_id="test_router", category="router", dependencies=(), api_version=API_VERSION):
    return PluginMetadata(id=plugin_id, category=category, api_version=api_version, plugin_version="0.1.0", dependencies=dependencies, capabilities=PluginCapabilities(routing_modes=["fixed"]) if category == "router" else PluginCapabilities())


class RegistryManagerTests(unittest.TestCase):
    def test_duplicate_registration_and_unknown_factory(self):
        registry = PluginRegistry()
        registry.register(metadata(), router_factory, config_model=EmptySettings)
        with self.assertRaises(DuplicatePluginError):
            registry.register(metadata(), router_factory, config_model=EmptySettings)
        with self.assertRaises(PluginNotFound):
            registry.get("router", "missing")

    def test_api_mismatch_and_invalid_plugin_version(self):
        registry = PluginRegistry()
        with self.assertRaises(PluginVersionError):
            registry.register(metadata(api_version="2.0"), router_factory, config_model=EmptySettings)
        with self.assertRaises(PluginVersionError):
            registry.register(metadata().replace(plugin_version="latest"), router_factory, config_model=EmptySettings)
        self.assertEqual(registry.descriptors(), ())

    def test_invalid_factory_and_dependency_declaration(self):
        registry = PluginRegistry()
        async def asynchronous_factory(*, config, dependencies):
            return TestRouter()
        for factory in [asynchronous_factory, lambda value: value, None]:
            with self.subTest(factory=factory):
                with self.assertRaises(PluginRegistrationError):
                    registry.register(metadata(), factory, config_model=EmptySettings)
        with self.assertRaises(PluginRegistrationError):
            registry.register(metadata(dependencies=["required"]), router_factory, config_model=EmptySettings)

    def test_missing_external_dependency_before_factory_execution(self):
        registry = PluginRegistry()
        calls = []
        def factory(*, config, dependencies):
            calls.append(True)
            return TestRouter()
        registry.register(metadata(dependencies=["service"]), factory, config_model=EmptySettings, dependency_types={"service": str})
        binding = PluginBinding(name="router", category="router", plugin_id="test_router", config={}, dependency_bindings={"service": "missing"})
        with self.assertRaisesRegex(DependencyError, "missing"):
            PluginManager(registry).build([binding])
        self.assertEqual(calls, [])
        with self.assertRaisesRegex(DependencyError, "requires str"):
            PluginManager(registry).build([binding], dependencies={"missing": 3})
        self.assertEqual(calls, [])

    def test_extra_or_unbound_dependencies_are_rejected(self):
        registry = PluginRegistry()
        registry.register(metadata(dependencies=["service"]), router_factory, config_model=EmptySettings, dependency_types={"service": str})
        for bindings in [{}, {"service": "service", "extra": "service"}]:
            with self.subTest(bindings=bindings):
                with self.assertRaises(DependencyError):
                    PluginManager(registry).build([PluginBinding(name="router", category="router", plugin_id="test_router", config={}, dependency_bindings=bindings)], dependencies={"service": "ok"})

    def test_dependency_method_signatures_are_checked(self):
        class BadStore:
            def put(self):
                return None
            def get(self):
                return None
        registry = PluginRegistry()
        registry.register(metadata(dependencies=["store"]), router_factory, config_model=EmptySettings, dependency_types={"store": ArtifactStore})
        binding = PluginBinding(name="router", category="router", plugin_id="test_router", config={}, dependency_bindings={"store": "store"})
        with self.assertRaisesRegex(DependencyError, "incompatible dependency interface"):
            PluginManager(registry).build([binding], dependencies={"store": BadStore()})

    def test_cyclic_dependencies_are_detected_before_factories(self):
        registry = PluginRegistry()
        calls = []
        def factory(*, config, dependencies):
            calls.append(True)
            return TestRouter()
        registry.register(metadata(dependencies=["router"]), factory, config_model=EmptySettings, dependency_types={"router": RouterPlugin})
        bindings = [PluginBinding(name="one", category="router", plugin_id="test_router", config={}, dependency_bindings={"router": "two"}), PluginBinding(name="two", category="router", plugin_id="test_router", config={}, dependency_bindings={"router": "one"})]
        with self.assertRaisesRegex(DependencyError, "one -> two -> one"):
            PluginManager(registry).build(bindings)
        self.assertEqual(calls, [])

    def test_shared_plugin_dependency_is_created_once(self):
        registry = PluginRegistry()
        created, injected = [], []
        def store_factory(*, config, dependencies):
            store = MockArtifactStore()
            created.append(store)
            return store
        def dependent_factory(*, config, dependencies):
            injected.append(dependencies.get("store"))
            return TestRouter()
        registry.register(metadata("store", "artifact_store"), store_factory, config_model=EmptySettings)
        registry.register(metadata(dependencies=["store"]), dependent_factory, config_model=EmptySettings, dependency_types={"store": ArtifactStore})
        bindings = [PluginBinding(name="one", category="router", plugin_id="test_router", config={}, dependency_bindings={"store": "store"}), PluginBinding(name="two", category="router", plugin_id="test_router", config={}, dependency_bindings={"store": "store"}), PluginBinding(name="store", category="artifact_store", plugin_id="store", config={})]
        built = PluginManager(registry).build(bindings)
        self.assertEqual(len(created), 1)
        self.assertIs(injected[0], injected[1])
        self.assertIs(injected[0], built.instances["store"])
        self.assertEqual(built.manifest.instances[0].binding_name, "store")

    def test_invalid_config_prevents_all_factory_calls(self):
        registry = PluginRegistry()
        calls = []
        def factory(*, config, dependencies):
            calls.append(True)
            return TestRouter()
        registry.register(metadata(), factory, config_model=EmptySettings)
        bindings = [PluginBinding(name="valid", category="router", plugin_id="test_router", config={}), PluginBinding(name="invalid", category="router", plugin_id="test_router", config={"unknown": 1})]
        with self.assertRaisesRegex(PluginConfigurationError, "invalid.config.unknown"):
            PluginManager(registry).build(bindings)
        self.assertEqual(calls, [])

    def test_bad_contract_method_signature_and_async_method(self):
        class MissingMethod:
            pass
        class WrongSignature:
            def select(self):
                return None
        class AsyncRouter:
            async def select(self, context):
                return None
        for plugin_type in [MissingMethod, WrongSignature, AsyncRouter]:
            with self.subTest(plugin=plugin_type):
                registry = PluginRegistry()
                def factory(*, config, dependencies):
                    return plugin_type()
                registry.register(metadata(), factory, config_model=EmptySettings)
                with self.assertRaises(ContractError):
                    PluginManager(registry).build([PluginBinding(name="router", category="router", plugin_id="test_router", config={})])

    def test_factory_failure_has_plugin_identity(self):
        def factory(*, config, dependencies):
            raise ValueError("construction failed")
        registry = PluginRegistry()
        registry.register(metadata(), factory, config_model=EmptySettings)
        with self.assertRaises(PluginInstantiationError) as caught:
            PluginManager(registry).build([PluginBinding(name="router", category="router", plugin_id="test_router", config={})])
        self.assertEqual(caught.exception.to_info().plugin_id, "test_router")

    def test_duplicate_instance_names_and_external_collision(self):
        registry = PluginRegistry()
        registry.register(metadata(), router_factory, config_model=EmptySettings)
        binding = PluginBinding(name="router", category="router", plugin_id="test_router", config={})
        with self.assertRaises(DependencyError):
            PluginManager(registry).build([binding, binding])
        with self.assertRaises(DependencyError):
            PluginManager(registry).build([binding], dependencies={"router": "external"})

    def test_registered_declarations_are_derived_from_metadata(self):
        registry = create_registry()
        catalog = registry.configuration_catalog()
        self.assertEqual(len(catalog.declarations), 26)
        self.assertEqual(catalog.get("router", "mock_router_b").routing_modes, ("fixed", "manual"))
        self.assertEqual(catalog.get("evolution", "mock_prompt_evolver_a").compatible_agents, ("mock_agent_a", "mock_agent_b"))
        with self.assertRaises(DuplicatePluginError):
            register_mock_plugins(registry)

    def test_assembly_manifests_are_serializable_and_versioned(self):
        assembled = assemble_experiment(CONFIGS / "mock_fixed_a.yaml")
        manifest = assembled.built_plugins.manifest
        self.assertEqual(AssemblyManifest.model_validate_json(manifest.model_dump_json()), manifest)
        for entry in manifest.instances:
            self.assertEqual(entry.descriptor.metadata.api_version, API_VERSION)
            self.assertEqual(entry.descriptor.metadata.plugin_version, "0.1.0")
            self.assertIn("properties", entry.descriptor.config_schema)

    def test_config_only_switches_all_eight_plugin_combinations(self):
        source = yaml.safe_load((CONFIGS / "mock_fixed_a.yaml").read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "fixture.yaml"
            for agent in ["mock_agent_a", "mock_agent_b"]:
                for router in ["mock_router_a", "mock_router_b"]:
                    for evolver in ["mock_prompt_evolver_a", "mock_prompt_evolver_b"]:
                        with self.subTest(agent=agent, router=router, evolver=evolver):
                            source["agent"]["plugin"] = agent
                            source["routing"]["plugin"] = router
                            source["targets"]["default_operators"]["prompt"] = evolver
                            source["evolution"]["max_candidates_per_round"] = 2
                            path.write_text(yaml.safe_dump(source), encoding="utf-8")
                            assembled = assemble_experiment(path)
                            bundle = assembled.plugins
                            stable = bundle.agent.snapshot()
                            before = stable.model_dump_json()
                            live = run_context()
                            result = bundle.agent.run(task_view(), stable, live)
                            self.assertEqual(result.output["agent"], agent)
                            public_result = ExecutionView(**{name: result.model_dump()[name] for name in ExecutionView.model_fields})
                            public = context().replace(stable_snapshot=stable, execution=public_result, feedback=FeedbackView(score=0, success=False, reason_code="unknown", public_feedback="fixture", evidence_step_ids=[0]))
                            decision = bundle.router.select(public)
                            request = EvolutionRequest(attempt_id="fixture", round_id=1, context=public, decision=decision, operator_id=evolver, max_candidates=2, run_context=live)
                            response = bundle.operators["prompt"].evolve(request)
                            self.assertEqual(len(response.candidates), 2)
                            self.assertTrue(all(item.candidate_id.startswith(evolver) for item in response.candidates))
                            self.assertEqual(stable.model_dump_json(), before)

    def test_manual_template_constructs_a_supported_router(self):
        assembled = assemble_experiment(CONFIGS / "mock_manual.yaml")
        self.assertEqual(assembled.loaded_config.config.evolution.mode, "manual")
        self.assertIsNotNone(assembled.plugins.router)

    def test_disabled_mode_only_constructs_agent(self):
        source = yaml.safe_load((CONFIGS / "v0_1_no_evolution.yaml").read_text(encoding="utf-8"))
        source["agent"]["plugin"] = "mock_agent_a"
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "disabled.yaml"
            path.write_text(yaml.safe_dump(source), encoding="utf-8")
            assembled = assemble_experiment(path)
            self.assertEqual(set(assembled.built_plugins.instances), {"agent"})
            self.assertIsNone(assembled.plugins.router)
            self.assertEqual(dict(assembled.plugins.operators), {})

    def test_real_plugins_assemble_without_becoming_mocks(self):
        load_experiment_config(CONFIGS / "v0_1_fixed_prompt.yaml")
        assembled = assemble_experiment(CONFIGS / "v0_1_fixed_prompt.yaml")
        self.assertEqual(assembled.plugins.agent.snapshot().agent_plugin_id, "react_agent")
        self.assertEqual(assembled.plugins.operators["prompt"].plugin_id, "simple_prompt_evolver")
