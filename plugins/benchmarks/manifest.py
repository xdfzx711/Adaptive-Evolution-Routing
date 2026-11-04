"""Reusable, explicit task roles and per-task holdouts; no label-based selection."""

from hashlib import sha256
import json
from pathlib import Path
from typing import Literal

from pydantic import Field, JsonValue, model_validator

from aer_core.config.models import NonBlank, Settings, UniqueStrings


class HoldoutSelection(Settings):
    similar: UniqueStrings = Field(min_length=1)
    regression: UniqueStrings = Field(min_length=1)


class DatasetSource(Settings):
    path: NonBlank
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    source_split: NonBlank


class TaskManifest(Settings):
    schema_version: Literal["aer.task_manifest.v1"] = "aer.task_manifest.v1"
    benchmark_id: NonBlank
    sources: list[DatasetSource] = Field(min_length=1)
    roles: dict[NonBlank, Literal["dev", "similar", "regression", "test"]]
    validation_sets: dict[NonBlank, HoldoutSelection]
    holdout_reuse: Literal["allowed", "forbidden"] = "allowed"
    selection_policy: dict[str, JsonValue] = Field(default_factory=dict)

    @model_validator(mode="after")
    def valid_roles(self):
        development = {key for key, role in self.roles.items() if role == "dev"}
        if not development or set(self.validation_sets) != development:
            raise ValueError("validation_sets must cover exactly every dev task")
        if len({source.path for source in self.sources}) != len(self.sources):
            raise ValueError("dataset sources must be unique")
        for task_id, selection in self.validation_sets.items():
            for group in ("similar", "regression"):
                for key in getattr(selection, group):
                    if self.roles.get(key) != group:
                        raise ValueError(f"{task_id}.{group}: {key} must belong to the {group} pool")
        if self.holdout_reuse == "forbidden":
            assigned = [key for selection in self.validation_sets.values()
                        for key in (*selection.similar, *selection.regression)]
            if len(assigned) != len(set(assigned)):
                raise ValueError("holdout_reuse=forbidden: each holdout belongs to exactly one current task")
            reserved = {key for key, role in self.roles.items() if role in {"similar", "regression"}}
            if set(assigned) != reserved:
                raise ValueError("exclusive holdout pools must match the assigned holdouts exactly")
        return self

    @classmethod
    def read(cls, path):
        return cls.model_validate(read_json(Path(path)))


def read_json(path):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate JSON key: {key}")
            result[key] = value
        return result
    def nonfinite(value):
        raise ValueError(f"nonfinite JSON value: {value}")
    return json.loads(path.read_text(encoding="utf-8-sig"), object_pairs_hook=unique, parse_constant=nonfinite)


def file_digest(path):
    digest = sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def select_holdouts(categories, *, seed, task_count, similar_count, regression_count, test_ids=()):
    """Deterministic public-category stratification, independent of input ordering.

    Roles are globally disjoint. Similar examples share the current category;
    regression examples rotate across the representative reserved pool.
    """
    if min(task_count, similar_count, regression_count) < 1:
        raise ValueError("task and holdout counts must be positive")
    if set(categories) & set(test_ids):
        raise ValueError("test source IDs overlap development source IDs")
    order = lambda key: sha256(f"{seed}:{key}".encode()).hexdigest()
    groups = {}
    for key, category in categories.items():
        groups.setdefault(category, []).append(key)
    categories_sorted = sorted(groups)
    if not categories_sorted:
        raise ValueError("no development tasks available")
    similar, regression, available = {}, [], []
    roles = {key: "test" for key in test_ids}
    # Reserve enough examples per category to represent every category in regression.
    reserve = max(1, (regression_count + len(groups) - 1) // len(groups))
    remaining = {}
    for category in categories_sorted:
        ids = sorted(groups[category], key=order)
        if len(ids) <= similar_count + reserve:
            raise ValueError(f"not enough tasks in category {category}")
        similar[category] = ids[:similar_count]
        regression.extend(ids[similar_count:similar_count + reserve])
        remaining[category] = ids[similar_count + reserve:]
        roles.update({key: "similar" for key in similar[category]})
    # Round-robin avoids selecting only the largest category.
    for index in range(max(map(len, remaining.values()))):
        for category in categories_sorted:
            if index < len(remaining[category]):
                available.append(remaining[category][index])
    if len(available) < task_count:
        raise ValueError("not enough distinct development tasks after reserving holdouts")
    roles.update({key: "regression" for key in regression})
    selections = {}
    for index, key in enumerate(available[:task_count]):
        roles[key] = "dev"
        selected_regression = [regression[(index + offset) % len(regression)] for offset in range(regression_count)]
        selections[key] = HoldoutSelection(similar=similar[categories[key]], regression=selected_regression)
    return roles, selections
