"""Inline the actual routing call in existing researcher-facing reports."""

import json

from aer_core.schemas.routing import JevRoutingRecord, RoutingRecord


def routing_record(proposal, artifacts):
    ref = proposal.decision.record_ref if proposal.decision else None
    error = proposal.response.error if proposal.response else None
    if ref is None and error is not None and error.stage == "routing":
        ref = error.evidence_ref
    if ref is None:
        return None
    if ref.kind == "routing_record":
        return RoutingRecord.model_validate_json(artifacts.get(ref))
    if ref.kind == "jev_routing_record":
        return JevRoutingRecord.model_validate_json(artifacts.get(ref))
    return None


def routing_view(record):
    if isinstance(record, JevRoutingRecord):
        data = record.model_dump(mode="json")
        return {"provider": "jev", "source": record.source,
                "request": data["request"], "response": data["response"],
                "usage": record.usage.model_dump(mode="json"),
                "error": {"code": record.error.code, "message": record.error.message} if record.error else None}
    messages = []
    for message in record.request.messages:
        content = message.content
        if message.role == "user":
            try:
                content = json.loads(content)
            except (ValueError, TypeError):
                pass
        messages.append({"role": message.role, "content": content})
    return {"messages": messages, "seed": record.request.seed, "max_output_tokens": record.request.max_output_tokens,
            "response": {"content": record.response.content, "finish_reason": record.response.finish_reason} if record.response else None,
            "usage": record.usage.model_dump(mode="json"),
            "error": {"code": record.error.code, "message": record.error.message} if record.error else None}
