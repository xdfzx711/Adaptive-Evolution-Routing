"""Optional model-based evaluators registered independently of orchestration."""

from aer_core.contracts.optimization import ModelProviderFactory
from aer_core.plugin_registry import API_VERSION
from aer_core.schemas.plugins import PluginMetadata
from .llm_patch import LLMPatchConfig, LLMPatchEvaluator


def build_llm_patch(*, config, dependencies):
    model = dependencies.get("model_factory").create(config.judge_model)
    return LLMPatchEvaluator(config, model)


def register_evaluator_plugins(registry):
    registry.register(PluginMetadata(id="llm_patch_evaluator", category="evaluator", api_version=API_VERSION,
        plugin_version="0.2.0", dependencies=["model_factory"]),
        build_llm_patch, config_model=LLMPatchConfig,
        dependency_types={"model_factory": ModelProviderFactory})
