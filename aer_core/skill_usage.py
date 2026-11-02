"""Check execution-bound skill exposure; never infer causal benefit from scores."""

from hashlib import sha256


def _body(document):
    if document is None:
        return None
    lines = document.content.splitlines(keepends=True)
    end = next(index for index in range(1, len(lines)) if lines[index].strip() == "---")
    return "".join(lines[end + 1:]).strip()


def skill_changes(before, after):
    old = {item.name: item for item in before.skills}
    new = {item.name: item for item in after.skills}
    changes = {}
    for name in sorted(old.keys() | new.keys()):
        left, right = old.get(name), new.get(name)
        if left == right:
            continue
        changes[name] = {"created": left is None, "removed": right is None,
            "body_changed": _body(left) != _body(right),
            "description_changed": (left.description if left else None) != (right.description if right else None)}
    return changes


def observe_skill(execution, snapshot, library, name):
    document = next((item for item in library.skills if item.name == name), None)
    expected_id = snapshot.artifacts["skill"].artifact_id
    expected_digest = sha256(document.content.encode("utf-8")).hexdigest() if document else None
    state = execution.model_dump(mode="json")["state_summary"] if execution else {}
    issues, reads = [], []
    observed_id = state.get("skill_library_artifact_id")
    if execution and execution.agent_version_id != snapshot.version_id:
        issues.append("execution_snapshot_mismatch")
    if observed_id is not None and observed_id != expected_id:
        issues.append("library_artifact_mismatch")
    verified = 0
    seen = set()
    receipts = state.get("skill_reads", [])
    if not isinstance(receipts, list):
        issues.append("malformed_read_receipts")
        receipts = []
    for receipt in receipts:
        if not isinstance(receipt, dict):
            issues.append("malformed_read_receipts")
            continue
        if receipt.get("name") != name or receipt.get("delivery") != "read" or receipt.get("status") != "completed":
            continue
        matches = bool(execution and document and isinstance(receipt.get("call_id"), str) and receipt["call_id"] and
            receipt.get("execution_operation_id") == execution.operation_id and
            receipt.get("snapshot_version_id") == snapshot.version_id and
            receipt.get("skill_library_artifact_id") == expected_id and
            receipt.get("content_digest") == expected_digest)
        reads.append({"call_id": receipt.get("call_id"), "content_digest": receipt.get("content_digest"),
            "snapshot_version_id": receipt.get("snapshot_version_id"),
            "skill_library_artifact_id": receipt.get("skill_library_artifact_id"),
            "execution_operation_id": receipt.get("execution_operation_id"), "matches_expected": matches})
        key = (receipt.get("execution_operation_id"), receipt.get("call_id"))
        if matches and key not in seen:
            verified += 1
        if matches:
            seen.add(key)
        if not matches:
            issues.append("read_receipt_mismatch")
    declared = name in state.get("read_skills", [])
    if declared and not reads:
        issues.append("declared_read_without_receipt")
    discovered = state.get("skill_discovered")
    available = name in state["available_skills"] if "available_skills" in state else None
    catalog_discovered = discovered if type(discovered) is bool else None
    discovered = False if catalog_discovered is False or available is False else (
        True if catalog_discovered is True and available is True else None)
    return {"expected_snapshot_version_id": snapshot.version_id, "expected_library_artifact_id": expected_id,
        "expected_content_digest": expected_digest, "observed_library_artifact_id": observed_id,
        "library_matches": observed_id == expected_id if observed_id is not None else None,
        "available": available, "catalog_discovered": catalog_discovered, "discovered": discovered,
        "read_declared": declared, "read_observed": bool(reads), "verified_read_count": verified,
        "read_verified": verified > 0 and not issues, "reads": reads,
        "preloaded": name in state.get("injected_skills", []), "issues": sorted(set(issues)),
        "evidence_complete": execution is not None and "skill_reads" in state and "malformed_read_receipts" not in issues
            and observed_id == expected_id and state.get("skill_setup_status") == "ready",
        "discovery_meaning": "MCP client discovered skill service, or catalog was submitted to the model; attention is not measured"}


def paired_skill_usage(old, new, baseline, candidate, before, after):
    skills = {}
    for name, change in skill_changes(before, after).items():
        left = observe_skill(old.execution, baseline, before, name)
        right = observe_skill(new.execution, candidate, after, name)
        if not change["body_changed"]:
            body_status = "not_applicable"
        elif right["read_verified"]:
            body_status = "verified_exposure_only_not_causal"
        elif right["read_observed"] or right["read_declared"]:
            body_status = "unverified_read_cannot_attribute"
        elif right["evidence_complete"]:
            body_status = "not_read_cannot_attribute"
        else:
            body_status = "unknown_cannot_attribute"
        selection = (right["read_verified"] != left["read_verified"]
            if left["evidence_complete"] and right["evidence_complete"] and not left["issues"] and not right["issues"] else None)
        skills[name] = {"change": change, "baseline": left, "candidate": right,
            "body_attribution": body_status, "selection_changed": selection,
            "description_attribution": "selection_comparison_only_not_causal" if change["description_changed"] else "not_applicable"}
    mismatches = {"execution_snapshot_mismatch", "library_artifact_mismatch", "read_receipt_mismatch", "malformed_read_receipts"}
    integrity = not any(mismatches.intersection(item[side]["issues"]) for item in skills.values() for side in ("baseline", "candidate"))
    return {"schema_version": "aer.paired_skill_usage.v1", "skills": skills, "integrity_ok": integrity,
        "score_delta": new.evaluation.score - old.evaluation.score if old.evaluation and new.evaluation else None,
        "baseline_cached": old.cache_hit, "candidate_cached": new.cache_hit,
        "attribution": "Descriptive evidence only. Unread bodies cannot explain score gains; verified reads do not prove use or causal benefit. Description changes may affect selection."}
