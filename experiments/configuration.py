"""Composition boundary: supply plugin metadata to the independent config loader."""

from pathlib import Path

from aer_core.config import LoadedConfig, load_config
from plugins.catalog import v0_1_catalog
from .bootstrap import create_registry
from .environment import resolve_model


def load_experiment_config(
    path: str | Path, *, benchmark_index: str | Path | None = None
) -> LoadedConfig:
    return resolve_model(load_config(path, catalog=create_registry().configuration_catalog(v0_1_catalog()), benchmark_index=benchmark_index))
