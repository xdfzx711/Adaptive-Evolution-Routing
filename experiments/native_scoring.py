"""Opt-in benchmark scoring contracts shared by outer execution and optimizers."""

from aer_core.contracts.benchmark import NativeEvaluationProvider
from aer_core.errors import ContractError


def validate_native_scoring(benchmark, config, registry, trial_override=None):
    if not isinstance(benchmark, NativeEvaluationProvider):
        return
    expected = benchmark.native_evaluator_id()
    if config.assessment.evaluator != expected:
        raise ContractError(f"Benchmark requires native evaluator {expected}", stage="setup")
    model = registry.get("evaluator", expected).config_model
    baseline = model.model_validate(config.assessment.evaluator_config)
    overrides = [trial_override] if trial_override is not None else [
        settings.get("trial_evaluator") for settings in config.targets.operator_configs.values()]
    for override in overrides:
        if override is None:
            continue
        plugin = override.plugin if hasattr(override, "plugin") else override["plugin"]
        settings = override.config if hasattr(override, "config") else override.get("config", {})
        if plugin != expected or model.model_validate(settings) != baseline:
            raise ContractError("Native benchmark requires identical inner and outer evaluator settings", stage="setup")
