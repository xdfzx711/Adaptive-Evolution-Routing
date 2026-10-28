"""Render optimizer receipts using the same views as outer round records."""

from aer_core.schemas.optimization import OptimizationRecord


def optimization_record(proposal, artifacts):
    ref = proposal.response.record_ref if proposal.response else None
    if ref is None or ref.kind != "evolution_optimization":
        return None
    return OptimizationRecord.model_validate_json(artifacts.get(ref))


def optimization_view(record):
    from .round_report import error_view, execution_view, fields, plain
    result = fields(record, "optimizer", "engine_version", "started_at", "finished_at", "status", "reason", "stop_reason", "config", "usage")
    result["candidates"] = plain(record)["candidates"]
    result["decisions"] = plain(record)["decisions"]
    result["trials"] = [{**fields(item, "trial", "candidate", "started_at", "finished_at"),
        "task": plain(item.observation.task), "execution": execution_view(item.observation.execution),
        "evaluation": plain(item.observation.feedback), "usage": plain(item.observation.usage),
        "error": error_view(item.observation.error)} for item in record.trials]
    result["reflections"] = [{**fields(item, "call", "started_at", "finished_at", "request", "usage"),
        "response": fields(item.response, "content", "finish_reason", "metadata"),
        "error": error_view(item.error)} for item in record.reflections]
    result["error"] = error_view(record.error)
    return result
