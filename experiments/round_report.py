"""One self-contained, readable JSON record per evolution round.

These are researcher-facing reports, never feedback or resumable domain records.
The artifact/version stores remain responsible for immutable machine references.
"""

import base64
import json
from datetime import datetime

from aer_core.schemas.diagnosis import DiagnosisRecord
from aer_core.schemas.paired import ValidationEvidence
from plugins.storage._files import FileRoot, readable_json
from plugins.storage.layout import episode_directory
from .routing_report import routing_record, routing_view


def plain(value):
    return value.model_dump(mode="json") if hasattr(value, "model_dump") else value


def fields(value, *names):
    data = plain(value)
    return {name: data[name] for name in names if name in data} if data is not None else None


def error_view(error):
    return fields(error, "code", "stage", "message", "plugin_id", "usage")


def diagnosis_view(value):
    return fields(value, "source", "reason_code", "explanation", "observations", "hypotheses",
                  "evidence_step_ids", "confidence", "usage")


def execution_view(value):
    """Keep observed actions/results, without echoing assembled model requests.

    Outputs and tool arguments are business data: never strip their ID fields.
    Event sequence numbers remain useful for the diagnosis evidence references.
    """
    if value is None:
        return None
    data = plain(value)
    trace = []
    for event in data["trajectory"]:
        if event["kind"] == "model_request":
            continue
        payload = dict(event["payload"])
        for key in ("operation_id", "call_id", "agent_version_id"):
            payload.pop(key, None)
        if event["kind"] == "model_response":
            payload["tool_calls"] = [fields(call, "name", "arguments") for call in payload.get("tool_calls", [])]
        if payload.get("error"):
            payload["error"] = error_view(payload["error"])
        trace.append({"step": event["seq"], "time": event["timestamp"], "kind": event["kind"], "result": payload})
    result = fields(value, "status", "output", "state_summary", "usage")
    result["trace"] = trace
    if data.get("error"):
        result["error"] = error_view(data["error"])
    return result


def scored_view(value):
    return {"execution": execution_view(value.execution),
            "evaluation": fields(value.evaluation, "score", "success", "metrics", "reason_code", "feedback"),
            "usage": plain(value.usage), "error": error_view(value.error)}


def condition_view(value):
    result = fields(value, "dataset_version", "scorer_version", "model_runtime", "model_endpoint",
                    "model_timeout_seconds", "tool_timeout_seconds", "max_output_tokens")
    result["plugins"] = []
    for name in ("execution_plugins", "benchmark_plugins"):
        if "plugins" in value.get(name, {}):
            result["plugins"].extend(value[name]["plugins"])
        for instance in value.get(name, {}).get("instances", []):
            metadata = instance["descriptor"]["metadata"]
            result["plugins"].append({"plugin": metadata["id"], "version": metadata["plugin_version"],
                                      "category": metadata["category"]})
    return result


class RoundReport:
    def __init__(self, root, task, context, artifacts, *, config=None):
        self.files, self.task, self.artifacts = FileRoot(root), task, artifacts
        self.config = config
        # Multiple tasks, or repeated task IDs in one run, must not share folders.
        self.directory = episode_directory(task.task_id, context.episode_id)
        self.round = None
        self.data = None
        self.latest_execution = None
        self.latest_diagnosis = None
        self.candidates = {}
        self.readable_sources = {}

    @property
    def relative_path(self):
        return f"{self.directory}/round-{self.round:03d}/record.json"

    @property
    def path(self):
        return self.files.path(self.relative_path) if self.round is not None else None

    def start(self, number, *, started_at=None):
        if self.round == number:
            return
        self.round = number
        self.candidates = {}
        self.data = {"round": number, "started_at": started_at or datetime.now().astimezone().isoformat(),
                     "status": "in_progress", "task": plain(self.task),
                     "execution_before_evolution": self.latest_execution,
                     "diagnosis": self.latest_diagnosis, "candidates": []}
        if self.config is not None:
            self.data["acceptance_policy"] = fields(self.config.validation, "acceptance_policy", "policy_version",
                                                   "min_improvement", "max_regression_drop")

    def publish(self):
        if self.data is not None:
            with self.files.locked():
                self.files.replace(self.relative_path, readable_json(self.data))

    def link(self, ref):
        if ref is not None:
            self.readable_sources[ref.uri] = self.path

    def content(self, ref):
        raw = self.artifacts.get(ref)
        try:
            text = raw.decode("utf-8")
        except UnicodeError:
            return {"encoding": "base64", "content": base64.b64encode(raw).decode("ascii")}
        # Prompts remain verbatim text; structured components are readable objects.
        if ref.kind != "prompt":
            try:
                return json.loads(text)
            except ValueError:
                pass
        return text

    def history(self, items):
        result = []
        for item in items:
            candidates = []
            for index, candidate in enumerate(item.candidate_summaries, 1):
                candidates.append({"candidate": index, **fields(candidate, "reward", "delta_score", "delta_similar_score",
                    "delta_regression_score", "accepted", "selected", "committed", "reason")})
            result.append({"task": item.task_id, "round": item.round_id, "decision": fields(item.decision, "target", "rationale"),
                           "diagnosis": diagnosis_view(item.diagnosis), "candidates": candidates})
        return result

    def proposal(self, value, ref):
        self.start(value.round_id, started_at=value.attempt.started_at.isoformat() if value.attempt else None)
        self.link(ref)
        context = value.context
        if context:
            self.data.pop("execution_before_evolution", None)
            self.data["evaluation_before_evolution"] = self.latest_execution["evaluation"] if self.latest_execution else None
            self.data["input_to_evolver"] = {
                "execution": execution_view(context.execution), "feedback": plain(context.feedback),
                "diagnosis": diagnosis_view(context.diagnosis),
                "stable_components": {name: self.content(component) for name, component in context.stable_snapshot.artifacts.items()},
                "history": self.history(context.history), "feasible_targets": list(context.feasible_targets),
                "remaining_budget": plain(context.remaining_budget)}
        self.data["routing"] = {"decision": fields(value.decision, "target", "rationale"), "operator": value.operator_id}
        receipt = routing_record(value, self.artifacts)
        if receipt is not None:
            self.data["routing"]["model_call"] = routing_view(receipt)
        response = value.response
        self.data["generation"] = {"status": response.status.value if response else value.status,
            "usage": plain(value.attempt.usage) if value.attempt else None,
            "evolver_usage": plain(response.usage) if response else None,
            "error": error_view(response.error) if response else None}
        if value.attempt:
            self.link(value.attempt.pre_state_ref)
        if response:
            for index, candidate in enumerate(response.candidates, 1):
                entry = {"candidate": index, "target": candidate.target,
                         "rationale": candidate.metadata.get("rationale"),
                         "components": {name: self.content(update) for name, update in candidate.artifact_updates.items()},
                         "status": "generated", "selected": False, "committed": False}
                self.candidates[candidate.candidate_id] = entry
                self.data["candidates"].append(entry)
                for update in candidate.artifact_updates.values():
                    self.link(update)
        self.data["status"] = "generated" if self.candidates else "generation_error" if response and response.error else "empty_candidates"

    def validation(self, batch, ref):
        self.link(ref)
        reports = {report.candidate_id: report for report in batch.reports}
        for outcome in batch.outcomes:
            entry = self.candidates[outcome.candidate_id]
            entry.update(status=outcome.status.value, selected=outcome.selected,
                acceptance=fields(outcome.acceptance, "accepted", "reward", "reason", "delta_score", "delta_similar_score",
                                  "delta_regression_score", "policy_id", "policy_version"),
                validation_error=error_view(outcome.validation_error))
            report = reports.get(outcome.candidate_id)
            if report is None:
                continue
            self.link(outcome.validation_ref)
            tasks = []
            for evidence_ref in (report.local_result_ref, report.private_results_ref):
                if evidence_ref is None:
                    continue
                self.link(evidence_ref)
                evidence = ValidationEvidence.model_validate_json(self.artifacts.get(evidence_ref))
                conditions = plain(evidence)["conditions"]
                self.data["validation_conditions"] = condition_view(conditions)
                public_inputs = {task["task_id"]: task["input"] for task in conditions.get("tasks", [])}
                for pair in evidence.results:
                    tasks.append({"group": pair.group, "task": pair.baseline.task.task_id,
                        "input": public_inputs.get(pair.baseline.task.task_id),
                        "seed": pair.baseline.seed, "baseline_cached": pair.baseline_cached,
                        "baseline": scored_view(pair.baseline), "candidate": scored_view(pair.candidate)})
            entry["validation"] = {"status": report.status.value,
                "baseline_metrics": plain(report.baseline_metrics), "candidate_metrics": plain(report.candidate_metrics),
                "current_task_metrics": plain(report.local_metrics), "tasks": tasks,
                "usage": plain(report.usage), "error": error_view(report.error)}
        self.data["validation_usage"] = plain(batch.usage)
        self.data["status"] = "validation_incomplete" if any(item.status != "complete" for item in batch.reports) else "selected" if batch.selected_candidate_id else "no_candidate_selected"

    def record(self, kind, value, ref=None):
        if kind == "round_start":
            self.start(value)
        elif kind == "controller_execution":
            self.latest_execution = scored_view(value)
            if self.data is not None:
                # Controller executions after a round are mandatory commit retests.
                self.data["post_commit_execution"] = self.latest_execution
        elif kind == "diagnosis":
            self.latest_diagnosis = diagnosis_view(value)
            if self.data is not None:
                self.data["diagnosis"] = self.latest_diagnosis
                self.link(value.record_ref)
        elif kind == "evolution_proposal":
            self.proposal(value, ref)
        elif kind == "validation_batch" and self.data is not None:
            self.validation(value, ref)
        elif kind == "commit_outcome" and self.data is not None:
            entry = self.candidates[value.candidate_id]
            entry.update(status=value.status.value, committed=value.committed, commit_error=error_view(value.commit_error))
            self.data["status"] = "committed" if value.committed else "commit_failed"
        elif kind == "run_result" and self.data is not None:
            self.data["run_result"] = {**fields(value, "status", "stop_reason", "stop_stage", "final_evaluation_status", "usage"),
                "error": error_view(value.error), "final_output": plain(value.final_execution)["output"] if value.final_execution else None,
                "final_evaluation": fields(value.final_evaluation, "score", "success", "reason_code", "metrics")}
            self.data["finished_at"] = datetime.now().astimezone().isoformat()
            if value.error:
                self.data["status"] = value.stop_reason.value
            if value.error and value.error.stage == "diagnosis" and value.error.evidence_ref:
                receipt = DiagnosisRecord.model_validate_json(self.artifacts.get(value.error.evidence_ref))
                self.data["diagnosis"] = {"error": error_view(receipt.error), "usage": plain(receipt.usage),
                    "response": fields(receipt.response, "content", "finish_reason")}
                self.data["status"] = "diagnosis_error"
                self.link(value.error.evidence_ref)
        self.publish()
