"""Composition boundary: supply plugin metadata to the independent config loader."""

from pathlib import Path
from dataclasses import replace
import os
import re
from dotenv import dotenv_values

from aer_core.config import LoadedConfig, load_config
from aer_core.errors import ConfigError, ConfigIssue
from plugins.catalog import v0_1_catalog
from .bootstrap import create_registry
from .environment import resolve_model


def load_experiment_config(
    path: str | Path | LoadedConfig, *, benchmark_index: str | Path | None = None,
    overrides: tuple[str, ...] | list[str] = (),
) -> LoadedConfig:
    loaded = resolve_model(load_config(path, catalog=create_registry().configuration_catalog(v0_1_catalog()),
        benchmark_index=benchmark_index, overrides=overrides))
    return resolve_skills(loaded)


def resolve_skills(loaded: LoadedConfig) -> LoadedConfig:
    """Resolve Agent libraries and embeddings; null disables default skills."""
    # Plugin input paths belong to the composition boundary, not core YAML semantics.
    if loaded.config.agent.plugin in {"react_agent", "single_call_agent", "codex_cli_agent"}:
        settings = dict(loaded.config.agent.config)
        if "skills" not in settings:
            settings["skills"] = {"initial_library": str(Path(__file__).resolve().parent / "skills")}
            if loaded.config.agent.plugin == "single_call_agent":
                settings["skills"].update(mode="preload", preload=["task-output-contract"])
        skills = settings.get("skills")
        if skills and skills.get("initial_library"):
            source = Path(skills["initial_library"]).expanduser()
            if not source.is_absolute():
                source = loaded.source_path.parent / source
            settings["skills"] = {**skills, "initial_library": str(source.resolve())}
        memory = settings.get("memory")
        if "memory" in loaded.config.targets.allowed and memory is None:
            raise ConfigError(loaded.source_path, [ConfigIssue("agent.config.memory",
                "The memory target requires an explicit memory library and embedding configuration")])
        if memory is not None:
            memory = {**memory, "embedding": dict(memory["embedding"])}
            if memory.get("initial_library"):
                source = Path(memory["initial_library"]).expanduser()
                memory["initial_library"] = str((loaded.source_path.parent / source).resolve() if not source.is_absolute() else source.resolve())
            embedding = memory["embedding"]
            if embedding.get("dotenv_path"):
                source = Path(embedding["dotenv_path"]).expanduser()
                embedding["dotenv_path"] = str((loaded.source_path.parent / source).resolve() if not source.is_absolute() else source.resolve())
            match = re.fullmatch(r"\$\{([A-Za-z_][A-Za-z_0-9]*)\}", embedding["model_id"])
            if match:
                values = dotenv_values(embedding.get("dotenv_path"), interpolate=False) if embedding.get("dotenv_path") else {}
                value = os.environ.get(match[1], values.get(match[1]))
                if value and value.strip():
                    embedding["model_id"] = value.strip()
            settings["memory"] = memory
        loaded = replace(loaded, config=loaded.config.model_copy(update={
            "agent": loaded.config.agent.model_copy(update={"config": settings})}))
    return loaded
