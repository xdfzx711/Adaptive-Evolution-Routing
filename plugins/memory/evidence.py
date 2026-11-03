"""Verify reported memory exposure against the immutable validation Snapshot."""

from .library import decode_bank


def verify_exposure(execution, snapshot, artifacts):
    state = execution.model_dump(mode="json")["state_summary"]
    ref = snapshot.artifacts["memory"]
    bank = decode_bank(artifacts.get(ref))
    if state.get("memory_library_artifact_id") != ref.artifact_id or state.get("memory_snapshot_version_id") != snapshot.version_id:
        raise ValueError("Execution memory belongs to a different Snapshot/library")
    if state.get("memory_experience_count") != len(bank):
        raise ValueError("Execution memory count differs from the Snapshot")
    hits = state.get("memory_retrieval")
    expected = min(snapshot.config["memory"]["top_k"], len(bank))
    if not isinstance(hits, list) or len(hits) != expected:
        raise ValueError("Execution memory retrieval count differs from configuration")
    seen = set()
    for hit in hits:
        index = hit.get("entry_index")
        if type(index) is not int or not 0 <= index < len(bank) or index in seen:
            raise ValueError("Invalid retrieved experience index")
        seen.add(index)
        if hit.get("task_id") != bank[index]["task_id"] or hit.get("memory_items") != bank[index]["memory_items"]:
            raise ValueError("Injected memory content differs from the Snapshot")
