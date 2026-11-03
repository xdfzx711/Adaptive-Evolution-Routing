"""HotpotQA distractor tasks with private labels and explicit per-task holdouts."""

from functools import lru_cache
from hashlib import sha256
import json
from pathlib import Path

from aer_core.config.models import NonBlank, Settings
from aer_core.config.partitions import BenchmarkIndex, IndexTask
from aer_core.errors import BenchmarkError, UnsupportedCapability
from aer_core.plugin_registry import API_VERSION
from aer_core.schemas.feedback import TaskFeedbackPolicy
from aer_core.schemas.plugins import PluginMetadata
from aer_core.schemas.task import EvaluationSpec, Task, TaskView
from plugins.evaluators.hotpotqa import HotpotQAEvaluator, HotpotQAEvaluatorSettings
from .manifest import TaskManifest, file_digest, read_json


OUTPUT_SCHEMA = {"type": "object", "properties": {
    "answer": {"type": "string"},
    "supporting_facts": {"type": "array", "items": {"type": "array",
        "prefixItems": [{"type": "string"}, {"type": "integer", "minimum": 0}],
        "minItems": 2, "maxItems": 2}}},
    "required": ["answer", "supporting_facts"], "additionalProperties": False}


class HotpotQASettings(Settings):
    manifest_path: NonBlank


@lru_cache(maxsize=4)
def _selected_records(sources, selected_ids):
    """Cache serialized selected rows only; each plugin gets its own fresh objects.

    Sources include stat identities and pinned hashes, so changed inputs cannot
    silently reuse an old parse. Large full-dataset rows are not retained.
    """
    selected, seen = {}, set()
    for path, expected_hash, source_split, _size, _mtime, _ctime in sources:
        if file_digest(path) != expected_hash:
            raise ValueError(f"dataset checksum mismatch: {Path(path).name}")
        records = read_json(Path(path))
        if not isinstance(records, list):
            raise ValueError("HotpotQA source must be a JSON array")
        for row in records:
            key = row.get("_id") if isinstance(row, dict) else None
            if not isinstance(key, str) or not key or key in seen:
                raise ValueError("HotpotQA source IDs must be nonempty and globally unique")
            seen.add(key)
            if key in selected_ids:
                selected[key] = (json.dumps(row, ensure_ascii=False), source_split)
    if set(selected) != set(selected_ids):
        raise ValueError("manifest references missing source task IDs")
    return tuple((key, *value) for key, value in selected.items())


def validate_row(row):
    if not isinstance(row.get("question"), str) or not row["question"].strip() or not isinstance(row.get("answer"), str):
        raise ValueError("HotpotQA requires a question and a string gold answer")
    context = row.get("context")
    if not isinstance(context, list) or not context:
        raise ValueError("HotpotQA requires context paragraphs")
    paragraphs = {}
    for item in context:
        if (not isinstance(item, list) or len(item) != 2 or not isinstance(item[0], str)
                or not isinstance(item[1], list) or not all(isinstance(s, str) for s in item[1])
                or item[0] in paragraphs):
            raise ValueError("invalid or duplicate context paragraph")
        paragraphs[item[0]] = item[1]
    facts = row.get("supporting_facts")
    if not isinstance(facts, list) or not facts:
        raise ValueError("HotpotQA requires gold supporting facts")
    for item in facts:
        if (not isinstance(item, list) or len(item) != 2 or not isinstance(item[0], str)
                or type(item[1]) is not int or item[0] not in paragraphs
                or not 0 <= item[1] < len(paragraphs[item[0]])):
            raise ValueError("distractor supporting fact must reference an existing context sentence")


class HotpotQABenchmark:
    benchmark_id = "hotpotqa"

    def native_evaluator_id(self):
        return "hotpotqa_evaluator"

    def export_predictions(self, outputs):
        # Invalid/missing fields remain absent, matching the official scorer's
        # missing-prediction behavior. Do not fabricate answers or evidence.
        from plugins.evaluators.hotpotqa import valid_supporting_facts
        return {"answer": {key: value["answer"] for key, value in outputs.items()
                           if isinstance(value.get("answer"), str)},
                "sp": {key: value["supporting_facts"] for key, value in outputs.items()
                       if valid_supporting_facts(value.get("supporting_facts"))}}

    def __init__(self, settings, config_root):
        path = Path(settings.manifest_path).expanduser()
        path = (config_root / path).resolve() if not path.is_absolute() else path.resolve()
        try:
            manifest = TaskManifest.read(path)
            if manifest.benchmark_id != self.benchmark_id:
                raise ValueError("manifest benchmark_id must be hotpotqa")
            paths, sources = [], []
            for source in manifest.sources:
                source_path = (path.parent / source.path).resolve()
                stat = source_path.stat()
                paths.append(source_path)
                sources.append((str(source_path), source.sha256, source.source_split,
                                stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns))
            records = _selected_records(tuple(sources), frozenset(manifest.roles))
            self._tasks, self._specs = {}, {}
            self._splits = {"dev": [], "validation": [], "test": []}
            for key, serialized, source_split in records:
                row = json.loads(serialized)
                validate_row(row)
                role = manifest.roles[key]
                split = "validation" if role in {"similar", "regression"} else role
                metadata = {"source_id": key, "source_split": source_split, "split": split,
                            "category": row.get("type", "unknown")}
                if split == "validation":
                    metadata["validation_group"] = role
                task = Task(task_id=key, benchmark_id=self.benchmark_id, metadata=metadata,
                    input={"question": row["question"], "context": [
                        {"title": title, "sentences": [{"sentence_id": i, "text": text} for i, text in enumerate(sentences)]}
                        for title, sentences in row["context"]], "output_schema": OUTPUT_SCHEMA,
                        "instruction": "Answer using only the supplied paragraphs. Return a JSON object with answer and "
                            "supporting_facts as [original document title, zero-based sentence ID] pairs. "
                            "Do not browse the web or access other files. Keep the answer concise."},
                    eval_spec_ref="hotpotqa.official.v1:" + key)
                self._tasks[key] = task
                self._splits[split].append(task)
                self._specs[key] = EvaluationSpec(task=task.identity, scoring_method="hotpotqa.official.v1",
                    private_parameters={"answer": row["answer"], "supporting_facts": row["supporting_facts"]})
            # Explicit manifest order is the reproducible episode order.
            self._splits = {split: tuple(self._tasks[key] for key in manifest.roles
                if self._tasks[key] in tasks) for split, tasks in self._splits.items()}
            self._validation_sets = manifest.validation_sets
            self.dataset_version = "hotpotqa.distractor." + sha256(
                manifest.model_dump_json().encode()).hexdigest()[:20]
            self.source_paths = (path, *paths)
        except (OSError, ValueError, KeyError, TypeError) as error:
            raise BenchmarkError(f"Cannot load HotpotQA: {error}", stage="setup", plugin_id="hotpotqa_adapter") from error

    def tasks(self, split):
        if split not in self._splits:
            raise UnsupportedCapability("Unknown HotpotQA split", stage="setup")
        return self._splits[split]

    def task_view(self, task):
        if self._tasks.get(task.task_id) != task:
            raise BenchmarkError("Task does not belong to this HotpotQA snapshot", stage="setup")
        return TaskView(task_id=task.task_id, benchmark_id=self.benchmark_id,
                        input=task.model_dump(mode="json")["input"], public_metadata=dict(task.metadata))

    def evaluation_spec(self, task):
        self.task_view(task)
        return self._specs[task.task_id]

    def validation_tasks(self, task):
        self.task_view(task)
        selection = self._validation_sets[task.task_id]
        return tuple(self._tasks[key] for key in (*selection.similar, *selection.regression))

    def index(self):
        return BenchmarkIndex(benchmark_id=self.benchmark_id, splits={split: [
            IndexTask(task_id=task.task_id, source_id=task.task_id, metadata=dict(task.metadata)) for task in tasks]
            for split, tasks in self._splits.items()})

    def feedback_policy(self, task):
        self.task_view(task)
        return TaskFeedbackPolicy(task=task.identity, allow_evolution=task in self.tasks("dev"),
            output_fields=["answer", "supporting_facts"], metadata_fields=["source_id", "source_split", "split", "category"],
            reason_messages={"correct": "Both answer and supporting facts exactly match the official targets.",
                "incorrect_answer": "The answer does not exactly match the official target; partial credit may apply.",
                "incorrect_supporting_facts": "The answer matches, but the supporting fact set does not exactly match.",
                "invalid_output_format": "An answer string or correctly structured supporting_facts array is missing."})


def build_benchmark(*, config, dependencies):
    return HotpotQABenchmark(config, dependencies.get("config_root"))


def build_evaluator(*, config, dependencies):
    return HotpotQAEvaluator(config)


def register_hotpotqa(registry):
    registry.register(PluginMetadata(id="hotpotqa_adapter", category="benchmark", api_version=API_VERSION,
        plugin_version="0.1.0", dependencies=["config_root"],
        capabilities={"benchmark_id": "hotpotqa", "splits": ["dev", "validation", "test"]}),
        build_benchmark, config_model=HotpotQASettings, dependency_types={"config_root": Path})
    registry.register(PluginMetadata(id="hotpotqa_evaluator", category="evaluator", api_version=API_VERSION,
        plugin_version="0.1.0"), build_evaluator, config_model=HotpotQAEvaluatorSettings)
