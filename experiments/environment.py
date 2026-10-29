"""Load provider credentials at the composition boundary, without global writes."""

import os
import re
from dataclasses import replace
from pathlib import Path

from dotenv import dotenv_values
from pydantic import SecretStr

from aer_core.config.loader import LoadedConfig
from aer_core.errors import PluginConfigurationError
from plugins.catalog import QwenModelConfig
from plugins.providers.qwen import QwenCredentials


def env_file_path(loaded: LoadedConfig) -> Path | None:
    if loaded.config.providers.model.plugin != "qwen_model":
        return None
    config = QwenModelConfig.model_validate(loaded.config.providers.model.config)
    if config.dotenv_path is None:
        return None
    path = Path(config.dotenv_path).expanduser()
    return (loaded.source_path.parent / path).resolve() if not path.is_absolute() else path.resolve()


def env_file_paths(loaded: LoadedConfig) -> tuple[Path, ...]:
    """All declared provider env inputs, including operator and evaluator providers."""
    paths = {path for path in (env_file_path(loaded),) if path is not None}
    models = [config.get("reflection_model") for config in loaded.config.targets.operator_configs.values()]
    models.append(loaded.config.assessment.evaluator_config.get("judge_model"))
    for model in models:
        if isinstance(model, dict) and model.get("plugin") == "qwen_model":
            path = QwenModelConfig.model_validate(model.get("config", {})).dotenv_path
            if path is not None:
                value = Path(path).expanduser()
                paths.add((loaded.source_path.parent / value).resolve() if not value.is_absolute() else value.resolve())
    return tuple(sorted(paths))


def _environment_values(loaded: LoadedConfig) -> dict:
    path = env_file_path(loaded)
    try:
        return dotenv_values(path, encoding="utf-8-sig", interpolate=False) if path is not None and path.is_file() else {}
    except (OSError, UnicodeError, ValueError):
        raise PluginConfigurationError("Cannot load Qwen .env; check dotenv_path", stage="setup", plugin_id="qwen_model") from None


def resolve_model(loaded: LoadedConfig, *, required: bool = False) -> LoadedConfig:
    """Resolve the model name before creating controlled Snapshots or providers."""
    model = loaded.config.providers.model
    match = re.fullmatch(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}", model.model_id)
    if model.plugin != "qwen_model" or match is None:
        return loaded
    variable = match.group(1)
    values = _environment_values(loaded)
    name = os.environ.get(variable, values.get(variable))
    if not name or not name.strip():
        if required:
            raise PluginConfigurationError("Missing Qwen environment variable: " + variable, stage="setup", plugin_id="qwen_model")
        return loaded
    data = loaded.config.model_dump(mode="json")
    data["providers"]["model"]["model_id"] = name.strip()
    return replace(loaded, config=type(loaded.config).model_validate(data))


def provider_dependencies(loaded: LoadedConfig, bindings, *, supplied: dict | None = None) -> dict:
    result = {"model_settings": loaded.config.providers.model, "config_root": loaded.source_path.parent}
    result.update(supplied or {})
    if "model_factory" not in result and any("model_factory" in binding.dependency_bindings.values() for binding in bindings):
        from .bootstrap import create_registry
        from .optimization import ConfiguredModelFactory
        result["model_factory"] = ConfiguredModelFactory(loaded, create_registry())
    if not any(binding.category == "model_provider" and binding.plugin_id == "qwen_model" for binding in bindings):
        return result
    loaded = resolve_model(loaded, required=True)
    if not supplied or "model_settings" not in supplied:
        result["model_settings"] = loaded.config.providers.model
    if "model_credentials" in result:
        return result
    config = QwenModelConfig.model_validate(loaded.config.providers.model.config)
    try:
        values = _environment_values(loaded)
        key = os.environ.get(config.api_key_env, values.get(config.api_key_env))
        url = os.environ.get(config.base_url_env, values.get(config.base_url_env))
        missing = [name for name, value in ((config.api_key_env, key), (config.base_url_env, url)) if not value or not value.strip()]
        if missing:
            raise PluginConfigurationError("Missing Qwen environment variables: " + ", ".join(missing),
                                           stage="setup", plugin_id="qwen_model")
        result["model_credentials"] = QwenCredentials(api_key=SecretStr(key.strip()), base_url=url.strip())
    except PluginConfigurationError:
        raise
    except (OSError, UnicodeError, ValueError):
        raise PluginConfigurationError("Cannot load Qwen credentials; check dotenv_path and the HTTP(S) base URL",
                                       stage="setup", plugin_id="qwen_model") from None
    return result
