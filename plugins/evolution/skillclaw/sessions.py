"""Export public visible events, never invent model turns or hidden reasoning."""

import json


def export_session(evidence, config):
    truncations = []
    def text(value, label):
        result = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
        if len(result) > config.max_field_chars:
            truncations.append(label)
            result = result[:config.max_field_chars] + "\n[AER: truncated]"
        return result

    events = list(evidence.events)
    if len(events) > config.max_events:
        head = config.max_events // 2
        events = events[:head] + events[-(config.max_events - head):]
        truncations.append("events_middle")
    retained = {event.seq for event in events}
    calls, turns = {}, []
    task = text(evidence.task.model_dump(mode="json"), "task")
    for event in events:
        payload = event.model_dump(mode="json")["payload"]
        turn = {"prompt_text": task, "response_text": "", "prm_score": None,
                "source_event_seq": event.seq, "source_kind": event.kind,
                "read_skills": [{"skill_name": read.name} for read in evidence.skill_reads if read.event_seq == event.seq]}
        if event.kind == "tool_call":
            name = payload.get("name", "unknown")
            calls[payload.get("call_id")] = name
            turn["tool_calls"] = [{"id": payload.get("call_id"), "function": {"name": name,
                "arguments": text(payload.get("arguments", {}), f"arguments:{event.seq}")}}]
        elif event.kind == "tool_result":
            turn["tool_results"] = [{"tool_name": payload.get("name", calls.get(payload.get("call_id"), "unknown")),
                "tool_call_id": payload.get("call_id"), "has_error": payload.get("status") == "error" or bool(payload.get("error_observed")),
                "content": text(payload.get("output", {}), f"result:{event.seq}")}]
            observation = turn["tool_results"][0]
            # Upstream's trajectory renderer prints results only alongside a
            # tool call in the same turn. AER has separate event identities;
            # preserve the observation explicitly rather than invent a call.
            turn["response_text"] = ("[AER tool-result observation, not an Agent message] "
                + observation["tool_name"] + ": " + observation["content"])
            if turn["tool_results"][0]["has_error"]:
                turn["tool_errors"] = [{"tool_name": observation["tool_name"],
                    "content": "Observed tool failure at event " + str(event.seq)}]
        elif event.kind == "model_response":
            turn["response_text"] = text(payload.get("content") or "", f"response:{event.seq}")
        elif event.kind == "model_request":
            # Public request events omit original prompts; do not reconstruct them.
            continue
        else:
            turn["response_text"] = "[AER observation, not an Agent message] " + text(
                {"kind": event.kind, "payload": payload}, f"observation:{event.seq}")
        # Native formatting numbers its own steps. Expose AER identities in the
        # rendered text as well, so diagnosis references remain interpretable.
        turn["response_text"] = f"[AER source event {event.seq}: {event.kind}] " + turn["response_text"]
        turns.append(turn)
    unaligned = [read for read in evidence.skill_reads if read.event_seq is None or read.event_seq not in retained]
    if unaligned or evidence.injected_skills or not turns:
        turns.append({"prompt_text": task, "prm_score": None, "source_kind": "session_evidence",
            "response_text": "[AER session-level evidence: actual read timing is unknown; this is not a chronological Agent step.]",
            "read_skills": [{"skill_name": read.name} for read in unaligned],
            "injected_skills": list(evidence.injected_skills)})
    # One real execution is one rollout. Outcome scores are never assigned as PRM.
    for turn in turns:
        turn.update(_rollout_idx=0, _rollout_score=evidence.feedback.score, _rollout_success=evidence.feedback.success)
    session = {"session_id": evidence.evidence_id, "turns": turns,
        "aggregate": {"rollout_count": 1, "scores": [evidence.feedback.score], "mean_score": evidence.feedback.score,
            "success_count": int(evidence.feedback.success), "fail_count": int(not evidence.feedback.success)},
        "aer_provenance": {"snapshot_version_id": evidence.snapshot_version_id,
            "skill_library_artifact_id": evidence.skill_library_artifact_id,
            "granularity": evidence.granularity, "gaps": list(evidence.gaps), "truncations": truncations}}
    return session
