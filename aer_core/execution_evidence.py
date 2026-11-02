"""Convert a disclosed EvolutionContext without reopening raw/private logs."""

from hashlib import sha256
import json

from aer_core.schemas.execution_evidence import ExecutionEvidence, SkillReadEvidence


def execution_evidence(context, library):
    snapshot = context.stable_snapshot
    artifact = snapshot.artifacts["skill"].artifact_id
    state = context.execution.model_dump(mode="json")["state_summary"]
    events = context.execution.trajectory
    documents = {skill.name: skill for skill in library.skills}
    reads, seen, gaps = [], {}, []
    if state.get("skill_setup_status") not in {None, "ready"}:
        raise ValueError("Skill service was not ready; this is not evidence of a missing skill")
    if state.get("skill_library_artifact_id", artifact) != artifact:
        raise ValueError("Execution used a different skill library")
    for item in state.get("skill_reads", []):
        if item.get("status") != "completed" or item.get("delivery") != "read":
            continue
        if item.get("snapshot_version_id") != snapshot.version_id or item.get("skill_library_artifact_id") != artifact:
            raise ValueError("Skill read evidence belongs to a different Snapshot/library")
        document = documents.get(item.get("name"))
        if document is None or sha256(document.content.encode("utf-8")).hexdigest() != item.get("content_digest"):
            raise ValueError("Skill read content does not match the frozen library")
        key = (item.get("execution_operation_id"), item.get("call_id"))
        if key in seen:
            if seen[key] != item:
                raise ValueError("Conflicting receipts for one skill read")
            continue
        seen[key] = item
        matches = []
        for event in events:
            if event.kind != "tool_result":
                continue
            payload = event.model_dump(mode="json")["payload"]
            output = payload.get("output", {})
            # Direct tools use AER call IDs; MCP supplies its own JSON-RPC ID.
            if payload.get("name") == "read_skill":
                observation = {**output, "call_id": payload.get("call_id")}
            else:
                result = output.get("result")
                observation = result.get("structuredContent", {}) if isinstance(result, dict) else {}
            if (isinstance(observation, dict) and observation.get("call_id") == item["call_id"]
                    and observation.get("content_digest") == item["content_digest"]
                    and observation.get("skill_library_artifact_id") == artifact
                    and observation.get("snapshot_version_id") == snapshot.version_id):
                matches.append(event.seq)
        seq = matches[0] if len(matches) == 1 else None
        if seq is None:
            gaps.append("skill_read_step_alignment_unknown")
        reads.append(SkillReadEvidence(name=item["name"], call_id=item["call_id"],
            execution_operation_id=item["execution_operation_id"], content_digest=item["content_digest"], event_seq=seq))
    if len({read.execution_operation_id for read in reads}) > 1:
        raise ValueError("Skill receipts span multiple executions")
    if set(state.get("read_skills", [])) - {read.name for read in reads}:
        gaps.append("declared_reads_without_verified_receipts")
    data = {"task": context.task.model_dump(mode="json"), "execution": context.execution.model_dump(mode="json")}
    identity = sha256(json.dumps(data, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()
    return ExecutionEvidence(evidence_id="execution-" + identity, snapshot_version_id=snapshot.version_id,
        skill_library_artifact_id=artifact, task=context.task, output=context.execution.output,
        usage=context.execution.usage, events=events, skill_reads=reads,
        injected_skills=state.get("injected_skills", []), feedback=context.feedback, diagnosis=context.diagnosis,
        gaps=sorted(set(gaps)))
