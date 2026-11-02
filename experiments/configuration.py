"""Composition boundary: supply plugin metadata to the independent config loader."""

from pathlib import Path
from dataclasses import replace

from aer_core.config import LoadedConfig, load_config
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
    """Apply experiment defaults; explicit null disables skills, {} stays empty."""
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
        loaded = replace(loaded, config=loaded.config.model_copy(update={
            "agent": loaded.config.agent.model_copy(update={"config": settings})}))
    return loaded
