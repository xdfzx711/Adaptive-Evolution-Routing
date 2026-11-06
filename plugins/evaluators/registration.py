"""Optional model-based evaluators registered independently of orchestration."""

from pathlib import Path

from aer_core.contracts.optimization import ModelProviderFactory
from aer_core.plugin_registry import API_VERSION
from aer_core.schemas.plugins import PluginMetadata
from .llm_patch import LLMPatchConfig, LLMPatchEvaluator


def build_llm_patch(*, config, dependencies):
    model = dependencies.get("model_factory").create(config.judge_model)
    return LLMPatchEvaluator(config, model)


def register_evaluator_plugins(registry):
    from .ifbench import IFBenchEvaluatorSettings, build_ifbench_evaluator
    registry.register(PluginMetadata(id="ifbench_evaluator", category="evaluator", api_version=API_VERSION,
        plugin_version="0.1.0", dependencies=["config_root"]), build_ifbench_evaluator,
        config_model=IFBenchEvaluatorSettings, dependency_types={"config_root": Path})
    registry.register(PluginMetadata(id="llm_patch_evaluator", category="evaluator", api_version=API_VERSION,
        plugin_version="0.2.0", dependencies=["model_factory"]),
        build_llm_patch, config_model=LLMPatchConfig,
        dependency_types={"model_factory": ModelProviderFactory})
