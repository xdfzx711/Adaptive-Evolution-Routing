"""Official IFBench checks; lazy optional dependency, no model-based grading."""

from hashlib import sha256
from functools import lru_cache
from importlib.metadata import version
from importlib.util import find_spec
import json
from pathlib import Path
from time import monotonic

from aer_core.config.models import NonBlank, Settings
from aer_core.errors import EvaluationError, PluginConfigurationError
from aer_core.identifiers import timestamp_id
from aer_core.schemas.execution import EvaluationResult, PublicCheckResult
from aer_core.schemas.scoring import MetricDefinition
from aer_core.schemas.usage import ResourceUsage


UPSTREAM_COMMIT = "cd189a9c2889526d8af901d2103fd3e1ffcea6df"


class IFBenchEvaluatorSettings(Settings):
    nltk_data_path: NonBlank | None = None


NLTK_RESOURCES = ("tokenizers/punkt", "tokenizers/punkt_tab", "corpora/stopwords",
                  "taggers/averaged_perceptron_tagger_eng")


def require_ifbench(nltk_data_path=None):
    try:
        import nltk
        spec = find_spec("ifbench")
        if spec is None:
            raise ImportError("ifbench")
        paths = ([str(Path(nltk_data_path).resolve())] if nltk_data_path else [])
        paths += [str(Path.cwd() / ".aer-cache/ifbench/nltk_data"),
                  str(Path(spec.origin).parent / ".nltk_data")]
        for path in reversed(paths):
            if path not in nltk.data.path:
                nltk.data.path.insert(0, path)
        missing = []
        for resource in NLTK_RESOURCES:
            try:
                nltk.data.find(resource)
            except LookupError:
                missing.append(resource)
        if missing:
            raise PluginConfigurationError("IFBench requires local NLTK resources: " + ", ".join(missing)
                + ". Run python -m experiments.prepare_ifbench_resources before evaluation.",
                stage="setup", plugin_id="ifbench_evaluator")
        # The upstream package attempts downloads at import; all checks above
        # must pass first so evaluation never initiates an implicit download.
        import ifbench
        from .ifbench_official import evaluation_lib
        # Upstream prepends its bundled path on import. Restore the explicit
        # search order before scoring and fingerprint the resources actually used.
        for path in reversed(paths):
            if path in nltk.data.path:
                nltk.data.path.remove(path)
            nltk.data.path.insert(0, path)
    except ImportError as error:
        raise PluginConfigurationError("IFBench evaluation requires the optional [ifbench] dependencies",
                                       stage="setup", plugin_id="ifbench_evaluator") from error
    return ifbench, evaluation_lib


@lru_cache(maxsize=4)
def _resource_content_digest(sources):
    digest = sha256()
    for name, path, _size, _mtime, _ctime in sources:
        digest.update(name.encode())
        digest.update(Path(path).read_bytes())
    return digest.digest()


def resource_fingerprint():
    import nltk
    sources = []
    for resource in NLTK_RESOURCES:
        pointer = nltk.data.find(resource)
        if hasattr(pointer, "zipfile"):
            paths = [(resource, Path(pointer.zipfile.filename))]
        else:
            root = Path(str(pointer))
            paths = [(resource + "/" + path.relative_to(root).as_posix(), path)
                     for path in sorted(root.rglob("*")) if path.is_file()]
        for name, path in paths:
            stat = path.stat()
            sources.append((name, str(path), stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns))
    return _resource_content_digest(tuple(sources))


def score_response(response, parameters, official):
    """Mirror the official strict-then-loose sequence on a fresh mutable input."""
    ids, kwargs, prompt = (parameters[name] for name in ("instruction_id_list", "kwargs", "prompt"))
    if (not isinstance(prompt, str) or not ids or len(ids) != len(kwargs)
            or any(not isinstance(item, str) for item in ids)):
        raise ValueError("IFBench requires a prompt and aligned nonempty instruction IDs/kwargs")
    # Private spec mappings are immutable. Strict evaluation removes None kwargs.
    # Copy recursively without changing the response or the benchmark specification.
    from aer_core.schemas.base import _plain
    inp = official.InputExample(key=parameters.get("key", 0), prompt=prompt,
        instruction_id_list=list(ids), kwargs=json.loads(json.dumps(_plain(kwargs))))
    predictions = {prompt: response} if isinstance(response, str) else {}
    strict = official.test_instruction_following_strict(inp, predictions)
    loose = official.test_instruction_following_loose(inp, predictions)
    total = len(ids)
    metrics = {"prompt_level_strict": float(strict.follow_all_instructions),
               "prompt_level_loose": float(loose.follow_all_instructions),
               "instruction_level_strict": sum(strict.follow_instruction_list) / total,
               "instruction_level_loose": sum(loose.follow_instruction_list) / total,
               "instruction_count": float(total),
               "instruction_strict_passed": float(sum(strict.follow_instruction_list)),
               "instruction_loose_passed": float(sum(loose.follow_instruction_list))}
    for index, instruction_id in enumerate(ids):
        metrics[f"constraint.{index}.{instruction_id}.strict"] = float(strict.follow_instruction_list[index])
        metrics[f"constraint.{index}.{instruction_id}.loose"] = float(loose.follow_instruction_list[index])
    return metrics


class IFBenchEvaluator:
    plugin_id = "ifbench_evaluator"
    success_policy = "prompt_level_loose == 1 (all instructions pass official loose checks)"
    score_policy = "Official IFBench prompt_level_loose (0 or 1); success iff all loose checks pass"

    def __init__(self, settings):
        package, self.official = require_ifbench(settings.nltk_data_path)
        root = Path(package.__file__).parent
        # Include actual checker source and relevant dependency versions in cache identity.
        digest = sha256()
        for path in sorted(root.rglob("*.py")):
            digest.update(path.relative_to(root).as_posix().encode())
            digest.update(path.read_bytes())
        self.runtime_versions = {name: version(name) for name in ("ifbench", "nltk", "langdetect", "emoji", "syllapy")}
        digest.update(json.dumps(self.runtime_versions, sort_keys=True).encode())
        digest.update(Path(self.official.__file__).read_bytes())
        digest.update(resource_fingerprint())
        # Check-level disclosure changes cached evaluation evidence, not native score arithmetic.
        digest.update(b"ifbench.public_checks.v1")
        self.scorer_version = f"ifbench.official.{UPSTREAM_COMMIT[:12]}.v1.{digest.hexdigest()[:20]}"

    def metric_definitions(self):
        return tuple(MetricDefinition(name=f"prompt_level_{mode}", direction="higher", minimum=0, maximum=1,
                kind="binary", aggregation="mean") for mode in ("strict", "loose")) + tuple(
            MetricDefinition(name=f"instruction_level_{mode}", direction="higher", minimum=0, maximum=1,
                kind="discrete", aggregation="ratio_of_sums", numerator=f"instruction_{mode}_passed",
                denominator="instruction_count") for mode in ("strict", "loose"))

    def evaluate(self, task, result, spec, run_ctx):
        started = monotonic()
        if (spec.task != task.identity or result.task_id != task.task_id
                or spec.scoring_method != "ifbench.official.v1"
                or result.status not in {"completed", "step_limit"}):
            raise EvaluationError("IFBench requires matching task/execution and native scoring spec", stage="evaluation")
        try:
            metrics = score_response(result.output.get("response"), spec.private_parameters, self.official)
        except Exception as error:
            # Missing resources and broken checkers are errors, never benchmark failures.
            raise EvaluationError(f"IFBench official checker failed ({type(error).__name__}): {error}",
                                  stage="evaluation") from error
        success = metrics["prompt_level_loose"] == 1
        reason = "correct" if success else "instruction_constraints_not_satisfied"
        return EvaluationResult(evaluation_id=timestamp_id("evaluation"), task_id=task.task_id,
            agent_version_id=result.agent_version_id, execution_operation_id=result.operation_id,
            operation_id=run_ctx.operation_id, score=metrics["prompt_level_loose"], success=success,
            metrics=metrics, feedback="Official IFBench evaluation: " + reason, reason_code=reason,
            public_checks=[PublicCheckResult(check_id=f"constraint.{i}.{name}",
                status="passed" if metrics[f"constraint.{i}.{name}.loose"] == 1 else "failed",
                requirement=f"Instruction constraint {i + 1}: {name}",
                explanation="Official IFBench loose checker result; this is a verdict, not a causal diagnosis.")
                for i, name in enumerate(spec.private_parameters["instruction_id_list"])],
            assessment_version=self.scorer_version,
            usage=ResourceUsage.zero().replace(elapsed_seconds=monotonic() - started))


def build_ifbench_evaluator(*, config, dependencies):
    if config.nltk_data_path:
        path = Path(config.nltk_data_path).expanduser()
        if not path.is_absolute():
            config = config.model_copy(update={"nltk_data_path": str((dependencies.get("config_root") / path).resolve())})
    return IFBenchEvaluator(config)
