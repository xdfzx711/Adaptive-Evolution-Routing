"""Descriptive tool-use statistics. No inference of usefulness or causal task improvement."""


def tool_usage_summary(state):
    available = list(state.get("available_tools", ()))
    metrics = state.get("tool_metrics")
    complete = metrics is not None
    # Old receipts lack durable totals; retain observed calls without claiming full coverage.
    if metrics is None:
        metrics = {}
        for call in state.get("tool_invocations", ()):
            value = metrics.setdefault(call["tool"], {"attempted": 0, "completed": 0, "execution_success": 0,
                "execution_error": 0, "business_success": 0, "business_failure": 0, "business_unknown": 0, "elapsed_seconds": 0.0})
            value["attempted"] += 1
            if call["status"] != "started":
                value["completed"] += 1
                value["execution_success" if call["status"] == "completed" else "execution_error"] += 1
                value["business_unknown"] += 1
                value["elapsed_seconds"] += call.get("elapsed_seconds") or 0
    tools = {}
    for name in sorted(set(available) | set(metrics)):
        count = dict(metrics.get(name, {}))
        attempts, completed = count.get("attempted", 0), count.get("completed", 0)
        success, failure = count.get("business_success", 0), count.get("business_failure", 0)
        tools[name] = {**count, "available": name in available, "attempted": attempts, "completed": completed,
            "pending": attempts - completed, "invoked": bool(attempts),
            "execution_success_rate": count.get("execution_success", 0) / completed if completed else None,
            "business_success_rate": success / (success + failure) if success + failure else None,
            "business_classified_calls": success + failure,
            "result_use_status": "not_assessed", "non_use_reason": None}
    return {"discovered": state.get("tool_discovered"), "discovery_meaning": "MCP client fetched tool list; model attention is not measured",
        "counts_complete": complete, "receipts_omitted": state.get("tool_invocations_omitted", 0), "tools": tools,
        "attribution": "descriptive_only; invocation does not establish causal task benefit"}
