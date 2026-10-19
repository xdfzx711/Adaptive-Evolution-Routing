"""Synchronous YAML loading, typed validation and path resolution."""

from dataclasses import dataclass
from pathlib import Path

import yaml
from pydantic import ValidationError
from yaml.constructor import ConstructorError

from aer_core.errors import ConfigError, ConfigIssue

from .catalog import ConfigCatalog
from .models import ExperimentConfig
from .partitions import load_benchmark_index, validate_partitions
from .validation import schema_issues, validate_semantics


class _UniqueKeyLoader(yaml.SafeLoader):
    pass


def _mapping(loader: _UniqueKeyLoader, node: yaml.MappingNode, deep: bool = False) -> dict:
    result = {}
    for key_node, value_node in node.value:
        if key_node.tag != "tag:yaml.org,2002:str":
            raise ConstructorError(None, None, "mapping keys must be strings; YAML merge keys are not supported", key_node.start_mark)
        key = loader.construct_object(key_node, deep=deep)
        if key in result:
            raise ConstructorError(None, None, f"duplicate YAML key: {key}", key_node.start_mark)
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


_UniqueKeyLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _mapping)


@dataclass(frozen=True)
class LoadedConfig:
    source_path: Path
    config: ExperimentConfig
    benchmark_index_path: Path | None

    def as_dict(self) -> dict:
        notes = ["Plugin references match declared v0.1 metadata; runtime plugins were not instantiated."]
        if self.benchmark_index_path is None:
            notes.append("Actual task membership, validation groups and split overlap were not checked; supply --benchmark-index or a future Benchmark Adapter.")
        if self.config.agent.initial_snapshot_ref is not None:
            notes.append("Snapshot file existence was checked; Snapshot contents and runtime compatibility need the future Version Store.")
        return {
            "status": "valid",
            "source_path": str(self.source_path),
            "benchmark_index_path": str(self.benchmark_index_path) if self.benchmark_index_path else None,
            "checks": {
                "schema": "passed",
                "semantics": "passed",
                "plugin_references": "declared_metadata_checked",
                "task_partitions": "index_checked" if self.benchmark_index_path else "split_names_and_mode_checked",
            },
            "notes": notes,
            "config": self.config.model_dump(mode="json"),
        }


def _resolve(path: str | Path, source: Path, field: str, base: Path | None = None) -> Path:
    try:
        value = Path(path).expanduser()
        if base is not None and not value.is_absolute():
            value = base / value
        return value.resolve()
    except (OSError, RuntimeError, ValueError) as error:
        raise ConfigError(source, [ConfigIssue(field, f"invalid path: {error}")]) from error


def load_config(
    path: str | Path, *, catalog: ConfigCatalog, benchmark_index: str | Path | None = None
) -> LoadedConfig:
    source = _resolve(path, Path(path), "$file")
    try:
        data = yaml.load(source.read_text(encoding="utf-8-sig"), Loader=_UniqueKeyLoader)
    except (OSError, UnicodeError, yaml.YAMLError) as error:
        raise ConfigError(source, [ConfigIssue("$file", str(error))]) from error
    if not isinstance(data, dict):
        raise ConfigError(source, [ConfigIssue("$", "YAML document must be a non-empty mapping")])
    try:
        config = ExperimentConfig.model_validate(data)
    except ValidationError as error:
        raise ConfigError(source, schema_issues(error)) from error
    issues = validate_semantics(config, catalog)
    if issues:
        raise ConfigError(source, issues)

    storage_root = _resolve(config.storage.root, source, "storage.root", source.parent)
    config = config.model_copy(update={"storage": config.storage.model_copy(update={"root": str(storage_root)})})
    if config.agent.initial_snapshot_ref is not None:
        snapshot_path = _resolve(config.agent.initial_snapshot_ref, source, "agent.initial_snapshot_ref", source.parent)
        if not snapshot_path.is_file():
            raise ConfigError(source, [ConfigIssue("agent.initial_snapshot_ref", f"Snapshot manifest file does not exist: {snapshot_path}")])
        config = config.model_copy(update={"agent": config.agent.model_copy(update={"initial_snapshot_ref": str(snapshot_path)})})

    index_path = None
    if benchmark_index is not None:
        index_path = _resolve(benchmark_index, source, "benchmark_index")
        index = load_benchmark_index(index_path)
        declaration = catalog.get("benchmark", config.benchmark.plugin)
        if declaration is None or declaration.benchmark_id is None:
            raise ConfigError(source, [ConfigIssue("benchmark.plugin", "Benchmark identity is missing from its declaration")])
        issues = validate_partitions(config, index, declaration.benchmark_id)
        if issues:
            raise ConfigError(index_path, issues)
    return LoadedConfig(source, config, index_path)
