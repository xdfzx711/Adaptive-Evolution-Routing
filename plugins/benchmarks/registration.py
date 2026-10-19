"""Factories for the local benchmark and deterministic evaluator."""

from aer_core.config.models import Settings
from aer_core.plugin_registry import API_VERSION, PluginRegistry, ResolvedDependencies
from aer_core.schemas.plugins import PluginCapabilities, PluginMetadata
from plugins.evaluators.deterministic import DeterministicEvaluator

from .toy_tasks import ToyBenchmark
from .hard_local import HardLocalBenchmark


class EmptyConfig(Settings):
    pass


def build_benchmark(*, config: EmptyConfig, dependencies: ResolvedDependencies) -> ToyBenchmark:
    return ToyBenchmark()


def build_evaluator(*, config: EmptyConfig, dependencies: ResolvedDependencies) -> DeterministicEvaluator:
    return DeterministicEvaluator()


def build_hard_benchmark(*, config: EmptyConfig, dependencies: ResolvedDependencies) -> HardLocalBenchmark:
    return HardLocalBenchmark()


def register_local_benchmark(registry: PluginRegistry) -> None:
    registry.register(PluginMetadata(id="hard_local", category="benchmark", api_version=API_VERSION,
        plugin_version="0.1.0", capabilities=PluginCapabilities(splits=("dev", "validation", "test"), benchmark_id="hard_local")),
        build_hard_benchmark, config_model=EmptyConfig)
    registry.register(PluginMetadata(id="toy_tasks", category="benchmark", api_version=API_VERSION,
        plugin_version="0.1.0", capabilities=PluginCapabilities(splits=("dev", "validation", "test"), benchmark_id="toy_tasks")),
        build_benchmark, config_model=EmptyConfig)
    registry.register(PluginMetadata(id="deterministic_evaluator", category="evaluator", api_version=API_VERSION,
        plugin_version="0.1.0"), build_evaluator, config_model=EmptyConfig)
