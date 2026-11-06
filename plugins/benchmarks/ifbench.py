"""Local IFBench tasks; checker parameters remain private, pairings are explicit."""

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
from .manifest import TaskManifest, file_digest
from .ifbench_selection import validate_rows, validate_selection


class IFBenchSettings(Settings):
    manifest_path: NonBlank


class IFBenchBenchmark:
    benchmark_id = "ifbench"

    def __init__(self, settings, config_root):
        path = Path(settings.manifest_path).expanduser()
        path = (config_root / path).resolve() if not path.is_absolute() else path.resolve()
        try:
            manifest = TaskManifest.read(path)
            if manifest.benchmark_id != self.benchmark_id:
                raise ValueError("manifest benchmark_id must be ifbench")
            rows, paths, origins = [], [], {}
            for source in manifest.sources:
                source_path = (path.parent / source.path).resolve()
                if file_digest(source_path) != source.sha256:
                    raise ValueError("IFBench source checksum mismatch")
                records = [json.loads(line) for line in source_path.read_text(encoding="utf-8").splitlines() if line.strip()]
                validate_rows(records)
                rows.extend(records)
                origins.update({str(row["key"]): source.source_split for row in records})
                paths.append(source_path)
            validate_rows(rows)
            validate_selection(rows, manifest)
            by_id = {str(row["key"]): row for row in rows}
            self._tasks, self._specs = {}, {}
            for key, role in manifest.roles.items():
                row = by_id[key]
                split = "validation" if role in {"similar", "regression"} else role
                metadata = {"source_id": key, "source_split": origins[key], "split": split,
                    "category": "instruction_following"}
                if split == "validation":
                    metadata["validation_group"] = role
                task = Task(task_id=key, benchmark_id=self.benchmark_id, metadata=metadata,
                    input={"prompt": row["prompt"]}, eval_spec_ref="ifbench.official.v1:" + key)
                self._tasks[key] = task
                self._specs[key] = EvaluationSpec(task=task.identity, scoring_method="ifbench.official.v1",
                    private_parameters={name: row[name] for name in ("key", "prompt", "instruction_id_list", "kwargs")})
            self._splits = {split: tuple(t for t in self._tasks.values() if t.metadata["split"] == split)
                for split in ("dev", "validation", "test")}
            self._validation_sets = manifest.validation_sets
            self.dataset_version = "ifbench." + sha256(manifest.model_dump_json().encode()).hexdigest()[:20]
            self.source_paths = (path, *paths)
        except (OSError, ValueError, KeyError, TypeError) as error:
            raise BenchmarkError(f"Cannot load IFBench: {error}", stage="setup", plugin_id="ifbench_adapter") from error

    def native_evaluator_id(self):
        return "ifbench_evaluator"

    def tasks(self, split):
        if split not in self._splits:
            raise UnsupportedCapability("Unknown IFBench split", stage="setup")
        return self._splits[split]

    def task_view(self, task):
        if self._tasks.get(task.task_id) != task:
            raise BenchmarkError("Task does not belong to this IFBench snapshot", stage="setup")
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
            IndexTask(task_id=t.task_id, source_id=t.task_id, metadata=dict(t.metadata)) for t in tasks]
            for split, tasks in self._splits.items()})

    def feedback_policy(self, task):
        self.task_view(task)
        return TaskFeedbackPolicy(task=task.identity, allow_evolution=task in self.tasks("dev"),
            public_check_ids=[f"constraint.{i}.{name}" for i, name in enumerate(
                self._specs[task.task_id].private_parameters["instruction_id_list"])],
            output_fields=["response"], metadata_fields=["source_id", "source_split", "split", "category"],
            reason_messages={"correct": "All instruction constraints pass the official loose checks.",
                "instruction_constraints_not_satisfied": "One or more instruction constraints in the prompt failed the official loose checks."})

    def export_predictions(self, outputs):
        return [{"key": self._specs[key].private_parameters["key"], "prompt": self._tasks[key].input["prompt"],
            "response": output["response"]} for key, output in outputs.items() if isinstance(output.get("response"), str)]


def build_benchmark(*, config, dependencies):
    return IFBenchBenchmark(config, dependencies.get("config_root"))


def register_ifbench(registry):
    registry.register(PluginMetadata(id="ifbench_adapter", category="benchmark", api_version=API_VERSION,
        plugin_version="0.1.0", dependencies=["config_root"],
        capabilities={"benchmark_id": "ifbench", "splits": ["dev", "validation", "test"]}),
        build_benchmark, config_model=IFBenchSettings, dependency_types={"config_root": Path})
