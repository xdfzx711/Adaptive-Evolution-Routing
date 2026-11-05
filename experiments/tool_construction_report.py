"""Compact observations of tool evolution; never used for scoring or acceptance."""

from collections import Counter
import json


def tool_library(snapshot, artifacts):
    ref = snapshot.artifacts.get("tool")
    if ref is None:
        return None
    value = json.loads(artifacts.get(ref))
    return {"artifact": ref.model_dump(mode="json"),
            "enabled_tools": value.get("enabled_tools", []),
            "generated_tools": sorted(value.get("generated_tools", {}))}


def construction_summary(record):
    receipt = record.get("generation", {}).get("tool_construction")
    if receipt is None:
        return None
    events = receipt.get("events", [])

    def names(kind, *, field="tool", **conditions):
        return sorted({event[field] for event in events if event.get("kind") == kind
                       and field in event and all(event.get(k) == v for k, v in conditions.items())})

    def last(kind):
        return next((event for event in reversed(events) if event.get("kind") == kind), {})

    return {"operator": receipt.get("operator"), "adapter_version": receipt.get("adapter_version"),
            "upstream_revision": receipt.get("upstream_revision"), "retrieval": receipt.get("retrieval"),
            "status": receipt["status"], "error": receipt.get("error"),
            "plan_completed": last("plan_finished").get("completed"),
            "completed_python_tools": names("node_completed", type="Python"),
            "export_selected_tools": names("export_selected"),
            "exported_tools": last("candidate_tools_prepared").get("tools", []),
            "delivery_verified_tools": names("delivery_check", passed=True),
            "export_failed_tools": names("export_failed"),
            "delivery_failed_tools": names("delivery_check", passed=False),
            "stop_events": [event["kind"] for event in events if event.get("kind") in {
                "graph_blocked", "repair_limit", "replan_limit", "construction_budget_reserved"}],
            "model_call_attempts_by_stage": dict(Counter(call["purpose"] for call in receipt.get("calls", []))),
            "usage": receipt.get("usage"), "timings": receipt.get("timings", {}),
            "scope": "Construction and delivery only; outer acceptance is recorded on candidates."}


def validation_tool_summary(validation):
    analysis = validation.get("tool_usage_analysis")
    if analysis is None:
        return None
    return {**{key: analysis[key] for key in ("attribution", "result_use_status", "cost_scope") if key in analysis},
            "groups": {name: {side: group[side] for side in ("baseline", "candidate")}
                       for name, group in analysis.get("groups", {}).items()}}


def aggregate_construction(entries):
    rounds = [row for entry in entries for row in entry.get("rounds", []) if row.get("tool_construction") is not None]
    if not rounds and not any(entry.get("initial_tool_library") is not None for entry in entries):
        return None
    counts = {key: sum(len(row["tool_construction"][key]) for row in rounds) for key in (
        "completed_python_tools", "exported_tools", "delivery_verified_tools", "export_failed_tools", "delivery_failed_tools")}
    candidates = [candidate for row in rounds for candidate in row.get("candidates", []) if candidate.get("target") == "tool"]
    return {"rounds": len(rounds), "statuses": dict(Counter(row["tool_construction"]["status"] for row in rounds)),
            "tool_counts_across_rounds": counts, "candidates": len(candidates),
            "accepted_candidates": sum(bool((candidate.get("acceptance") or {}).get("accepted")) for candidate in candidates),
            "committed_candidates": sum(bool(candidate.get("committed")) for candidate in candidates),
            "scope": "Counts are per episode/round, not globally distinct tools. Construction usage is already in episode usage."}
