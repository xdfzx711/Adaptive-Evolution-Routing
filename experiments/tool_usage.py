"""Researcher-facing paired usage/cost analysis; independent of acceptance and reward."""


def analyze_tool_usage(tasks):
    groups = {}
    for pair in tasks:
        group = groups.setdefault(pair["group"], {"tasks": [], "baseline": {}, "candidate": {}})
        row = {"task": pair["task"], "score_delta": None, "token_delta": None, "elapsed_delta": None}
        for side in ("baseline", "candidate"):
            scored = pair[side]
            usage = (scored.get("execution") or {}).get("tool_usage", {})
            for name, tool in usage.get("tools", {}).items():
                total = group[side].setdefault(name, {"tasks_available": 0, "tasks_invoked": 0, "counts_complete": True,
                    "attempted": 0, "completed": 0, "pending": 0, "execution_success": 0, "execution_error": 0,
                    "business_success": 0, "business_failure": 0, "business_unknown": 0, "business_not_evaluated": 0,
                    "elapsed_seconds": 0.0})
                total["tasks_available"] += bool(tool["available"])
                total["tasks_invoked"] += bool(tool["invoked"] and tool["available"])
                total["counts_complete"] &= usage.get("counts_complete", False)
                for key in ("attempted", "completed", "pending", "execution_success", "execution_error", "business_success",
                            "business_failure", "business_unknown", "business_not_evaluated", "elapsed_seconds"):
                    total[key] += tool.get(key, 0)
            row[side] = {"cached": pair.get(side + "_cached", False), "usage": usage,
                "score": (scored.get("evaluation") or {}).get("score"),
                "tokens": ((scored.get("execution") or {}).get("usage") or {}).get("total_tokens"),
                "elapsed_seconds": ((scored.get("execution") or {}).get("usage") or {}).get("elapsed_seconds")}
        for delta, field in (("score_delta", "score"), ("token_delta", "tokens"), ("elapsed_delta", "elapsed_seconds")):
            before, after = row["baseline"][field], row["candidate"][field]
            if before is not None and after is not None:
                row[delta] = after - before
        group["tasks"].append(row)
    for group in groups.values():
        for side in ("baseline", "candidate"):
            for total in group[side].values():
                known = total["business_success"] + total["business_failure"]
                total["task_invocation_rate"] = total["tasks_invoked"] / total["tasks_available"] if total["tasks_available"] else None
                total["execution_success_rate"] = total["execution_success"] / total["completed"] if total["completed"] else None
                total["business_success_rate"] = total["business_success"] / known if known else None
                total["business_classified_calls"] = known
    return {"groups": groups, "attribution": "Descriptive comparison, not causal benefit. No calls means no evidence of tool-code execution benefit.",
        "result_use_status": "not_assessed", "cost_scope": "Observed execution costs, including cached observations; not newly billed costs."}
