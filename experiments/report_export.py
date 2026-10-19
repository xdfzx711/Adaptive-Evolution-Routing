"""Offline reconstruction of completed episodes; never assembles a model provider."""

import io
import json
import re
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path

from aer_core.config.models import ExperimentConfig
from aer_core.schemas.experience import CandidateEvaluatedEvent, TaskFinishedEvent
from aer_core.schemas.experiment import RunResult
from aer_core.schemas.diagnosis import DiagnosisRecord
from aer_core.schemas.paired import ScoredExecution, ValidationBatchResult
from aer_core.schemas.proposal import EvolutionProposalRecord
from aer_core.schemas.references import ArtifactRef
from aer_core.schemas.runtime import RunContextRecord
from aer_core.schemas.task import TaskView
from plugins.storage._files import FileRoot, key_path, unique_json
from plugins.storage.artifacts import LocalArtifactStore
from plugins.storage.experience import JsonlExperienceStore
from plugins.storage.versions import LocalVersionStore
from plugins.storage.layout import RunLayout, read_manifest

from .evolution_report import EvolutionTextReport
from .round_report import RoundReport


def _ref(path, raw, kind):
    digest = sha256(raw).hexdigest()
    identity = "sha256-" + sha256(kind.encode("utf-8") + b"\0" + raw).hexdigest()
    return ArtifactRef(artifact_id=identity, kind=kind, version=digest, digest=digest,
        uri="artifacts/" + path.name)


def _file_time(path):
    stamp = re.search(r"\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}_\d{6}", path.stem)
    if stamp:
        return datetime.strptime(stamp.group(), "%Y-%m-%d_%H-%M-%S_%f").astimezone()
    return datetime.min.replace(tzinfo=timezone.utc)


def _records(root, episode_ids, artifacts):
    """Legacy artifacts have no episode index. Scan once and validate matching DTOs."""
    result = {key: [] for key in episode_ids}
    index = artifacts._index()
    entries = [(artifacts.files.path(ref["uri"]), ArtifactRef.model_validate(ref)) for ref in index.values()]
    if not entries:
        entries = [(path, None) for path in sorted((root / "artifacts").glob("*.bin"))]
    for path, stored_ref in entries:
        raw = artifacts.get(stored_ref) if stored_ref else path.read_bytes()
        try:
            data = json.loads(raw)
        except (ValueError, UnicodeError):
            continue
        if not isinstance(data, dict):
            continue
        kind, model = None, None
        if "execution" in data and "version_id" in data and "seed" in data:
            kind, model = "controller_execution", ScoredExecution
            operation = (data.get("execution") or {}).get("operation_id") or (data.get("error") or {}).get("operation_id") or ""
            episode = next((key for key in episode_ids if operation.startswith(key + "/")), None)
        elif "diagnosis" in data and "started_at" in data and "feedback" in data:
            kind, model, episode = "diagnosis", DiagnosisRecord, data.get("episode_id")
        elif "attempt" in data and "response" in data and "round_id" in data:
            kind, model, episode = "evolution_proposal", EvolutionProposalRecord, data.get("episode_id")
        elif "reports" in data and "outcomes" in data and "attempt_id" in data:
            kind, model, episode = "validation_batch", ValidationBatchResult, data.get("episode_id")
        else:
            continue
        if episode not in result:
            continue
        ref = stored_ref or _ref(path, raw, kind)
        if ref.kind != kind:
            continue
        # Verify content identity as well as the domain contract before rendering.
        value = model.model_validate_json(artifacts.get(ref))
        if kind == "diagnosis":
            timestamp = value.finished_at
        elif kind == "controller_execution":
            timestamps = [event.timestamp for event in value.execution.trajectory if event.timestamp] if value.execution else []
            timestamp = min(timestamps) if timestamps else _file_time(path)
        elif kind == "evolution_proposal":
            timestamp = value.attempt.finished_at if value.attempt else datetime.min.replace(tzinfo=timezone.utc)
        else:
            timestamp = max((item.final_recorded_at for item in value.outcomes), default=datetime.min.replace(tzinfo=timezone.utc))
        result[episode].append((timestamp, 0, kind, value, ref))
    return result


def export_saved_run(path: Path) -> Path:
    payload = unique_json(path.read_bytes())
    selected = RunResult.model_validate(payload["record"])
    root = Path(payload["run_root"]).expanduser().resolve()
    target = root / "evolution.txt"
    if target.exists():
        return target
    files = FileRoot(root)
    artifacts = LocalArtifactStore(root)
    events = [event for event in JsonlExperienceStore(root).events() if event.run_id == selected.run_id]
    results = {selected.episode_id: (selected, ArtifactRef.model_validate(payload["record_ref"]))}
    for event in events:
        if isinstance(event, TaskFinishedEvent) and event.episode_id not in results:
            if isinstance(event.payload, RunResult):
                results[event.episode_id] = (event.payload, None)
            else:
                result = RunResult.model_validate_json(artifacts.get(event.payload.result_ref))
                results[event.episode_id] = (result, event.payload.result_ref)
    config = ExperimentConfig.model_validate(read_manifest(root, "storage", "storage_manifest.json")["experiment_config"])
    execution_manifest = read_manifest(root, "execution", "execution_manifest.json")
    tasks = {task["task_id"]: TaskView.model_validate(task) for task in execution_manifest["benchmark"]["public_tasks"]}
    indexed = _records(root, set(results), artifacts)
    buffer = io.StringIO()
    order = sorted(results, key=lambda key: min((record[0] for record in indexed[key]), default=datetime.min.replace(tzinfo=timezone.utc)))
    for episode in order:
        result, ref = results[episode]
        manifest = files.read_json(RunLayout(root).episode_path(episode))
        versions = LocalVersionStore(root, artifacts, scope_id=manifest["scope_id"])
        context = RunContextRecord.model_validate(manifest["run_context"])
        snapshot = versions.load(result.initial_version_id)
        records = indexed[episode]
        for event in events:
            if (event.episode_id == episode and isinstance(event, CandidateEvaluatedEvent)
                and (event.payload.committed or event.payload.commit_error)):
                records.append((event.timestamp, 1, "commit_outcome", event.payload, None))
        records.sort(key=lambda item: (item[0], item[1]))
        report = EvolutionTextReport(buffer.write, artifacts, versions, root=root)
        rounds = RoundReport(root, tasks[result.task_id], context, artifacts, config=config)
        report.readable_sources = rounds.readable_sources
        report.begin(tasks[result.task_id], snapshot, config, context,
            started_at=records[0][0] if records else None)
        report.block("导出说明", ["根据已保存工件离线整理；不调用模型，不重新评分，不修改经验记录。",
            "未保存的诊断器原始解释不补写；各轮仍展示当轮过滤后的实际进化上下文。"])
        for record_time, _, kind, value, record_ref in records:
            if kind == "diagnosis":
                if value.diagnosis:
                    following = next((proposal.round_id for time, _, tag, proposal, _ in records
                        if tag == "evolution_proposal" and time >= record_time), (rounds.round or 0) + 1)
                    rounds.record("round_start", following)
                    rounds.record("diagnosis", value.diagnosis.replace(record_ref=record_ref), record_ref)
                    report.current_time = value.finished_at
                    report.record("diagnosis", value.diagnosis.replace(record_ref=record_ref), record_ref)
            else:
                rounds.record(kind, value, record_ref)
                report.record(kind, value, record_ref)
        attempts = [event for event in events if event.episode_id == episode and event.record_type == "attempt_started"]
        recorded = {value.attempt.attempt_id for _, _, kind, value, _ in records if kind == "evolution_proposal" and value.attempt}
        for event in attempts:
            if event.payload.attempt_id not in recorded:
                report.block("记录缺失", [f"第 {event.payload.round_id} 轮的生成工件缺失；尝试事件已保存。", event.payload.pre_state_ref.uri])
        rounds.record("run_result", result, ref)
        report.finish(result, ref)
    # Publish only a fully rendered report. Repeated export does not append duplicate episodes.
    with files.locked():
        if not target.exists():
            files.replace("evolution.txt", buffer.getvalue().encode("utf-8"))
    return target
