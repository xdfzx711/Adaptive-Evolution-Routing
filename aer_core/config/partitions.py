"""Optional public task identity index; no task execution or scoring data."""

import json
from pathlib import Path
from typing import Literal

from pydantic import JsonValue, ValidationError

from aer_core.errors import ConfigError, ConfigIssue

from .models import ExperimentConfig, NonBlank, Settings
from .validation import schema_issues


class IndexTask(Settings):
    task_id: NonBlank
    source_id: NonBlank | None = None
    metadata: dict[str, JsonValue]


class BenchmarkIndex(Settings):
    benchmark_id: NonBlank
    splits: dict[Literal["dev", "validation", "test"], list[IndexTask]]


def _unique_json_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def load_benchmark_index(path: Path) -> BenchmarkIndex:
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"), object_pairs_hook=_unique_json_keys)
    except (OSError, UnicodeError, ValueError) as error:
        raise ConfigError(path, [ConfigIssue("benchmark_index", str(error))]) from error
    try:
        return BenchmarkIndex.model_validate(data)
    except ValidationError as error:
        raise ConfigError(path, schema_issues(error, "benchmark_index")) from error


def validate_partitions(
    config: ExperimentConfig, index: BenchmarkIndex, expected_benchmark_id: str
) -> list[ConfigIssue]:
    issues: list[ConfigIssue] = []
    if index.benchmark_id != expected_benchmark_id:
        issues.append(ConfigIssue("benchmark_index.benchmark_id", f"must match {expected_benchmark_id!r}"))
    required_splits = {"dev", "validation", "test"}
    if set(index.splits) != required_splits:
        issues.append(ConfigIssue("benchmark_index.splits", "must include dev, validation and test"))
        return issues

    identities: dict[str, str] = {}
    sources: dict[str, str] = {}
    for split, tasks in index.splits.items():
        for position, task in enumerate(tasks):
            field = f"benchmark_index.splits.{split}[{position}]"
            if task.task_id in identities:
                issues.append(ConfigIssue(f"{field}.task_id", f"duplicate task identity already in {identities[task.task_id]}"))
            else:
                identities[task.task_id] = split
            if task.source_id is not None:
                if task.source_id in sources:
                    issues.append(ConfigIssue(f"{field}.source_id", f"duplicate source identity already in {sources[task.source_id]}"))
                else:
                    sources[task.source_id] = split

    tasks = index.splits[config.benchmark.execution_split]
    available_ids = {task.task_id for task in tasks}
    if not tasks:
        issues.append(ConfigIssue("benchmark.execution_split", "selected split has no tasks in the index"))
    if config.benchmark.task_ids is not None:
        for position, task_id in enumerate(config.benchmark.task_ids):
            if task_id not in available_ids:
                issues.append(ConfigIssue(f"benchmark.task_ids[{position}]", f"task {task_id!r} is not in the selected split"))

    if config.evolution.mode != "disabled":
        validation = config.validation
        counts = {validation.similar_group: 0, validation.regression_group: 0}
        for position, task in enumerate(index.splits[validation.split]):
            group = task.metadata.get(validation.group_field)
            if not isinstance(group, str) or group not in counts:
                field = f"benchmark_index.splits.{validation.split}[{position}].metadata.{validation.group_field}"
                issues.append(ConfigIssue(field, "must identify the similar or regression validation group"))
            else:
                counts[group] += 1
        for group, count in counts.items():
            if not count:
                issues.append(ConfigIssue("validation", f"validation group {group!r} must have at least one task"))
    return issues
