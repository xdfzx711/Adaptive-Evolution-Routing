"""Load provider credentials at the composition boundary, without global writes."""

import os
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


def _environment_values(loaded: LoadedConfig) -> dict:
    path = env_file_path(loaded)
    try:
        return dotenv_values(path, encoding="utf-8-sig", interpolate=False) if path is not None and path.is_file() else {}
    except (OSError, UnicodeError, ValueError):
        raise PluginConfigurationError("Cannot load Qwen .env; check dotenv_path", stage="setup", plugin_id="qwen_model") from None


def resolve_model(loaded: LoadedConfig, *, required: bool = False) -> LoadedConfig:
    """Resolve the model name before creating controlled Snapshots or providers."""
    model = loaded.config.providers.model
    if model.plugin != "qwen_model" or model.model_id != "${QWEN_MODEL}":
        return loaded
    values = _environment_values(loaded)
    name = os.environ.get("QWEN_MODEL", values.get("QWEN_MODEL"))
    if not name or not name.strip():
        if required:
            raise PluginConfigurationError("Missing Qwen environment variable: QWEN_MODEL", stage="setup", plugin_id="qwen_model")
        return loaded
    data = loaded.config.model_dump(mode="json")
    data["providers"]["model"]["model_id"] = name.strip()
    return replace(loaded, config=type(loaded.config).model_validate(data))


def provider_dependencies(loaded: LoadedConfig, bindings, *, supplied: dict | None = None) -> dict:
    result = {"model_settings": loaded.config.providers.model}
    result.update(supplied or {})
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
