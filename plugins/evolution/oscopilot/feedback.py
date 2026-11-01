"""Project public construction events into reusable feedback; never read evaluator artifacts."""

from aer_core.schemas.construction import ConstructionFeedback, ConstructionIssue


def construction_feedback(events, calls, status, error, record_ref, min_score):
    issues = []
    exported = []
    implementations = {}

    def add(stage, code, message, index=None, event=None, *, source="observation", details=None):
        event = event or {}
        issues.append(ConstructionIssue(stage=stage, code=code, tool=event.get("tool"), source=source,
            message=message, event_index=index, repair_index=event.get("repair"), details=details or {}))

    def resolve(tool, codes, index):
        event = events[index]
        resolution = {key: event[key] for key in ("source", "result", "status", "score", "reasoning", "proposed_nodes") if key in event}
        for position, item in enumerate(issues):
            if item.status == "unresolved" and item.tool == tool and item.code in codes:
                issues[position] = item.replace(status="resolved", resolution_event_index=index, resolution_details=resolution)

    for index, event in enumerate(events):
        kind, tool = event["kind"], event.get("tool")
        if kind == "plan_contract_check":
            # Read historical receipts from adapters before 1.10; new runs never emit this event.
            if event["accepted"]:
                resolve(None, {"plan_contract_conflict"}, index)
            else:
                add("planning", "invalid_plan_contract_review" if event.get("error") else "plan_contract_conflict",
                    event.get("error") or "Proposed descriptions, JSON interfaces or acceptance were not found compatible",
                    index, source="observation" if event.get("error") else "model_assessment",
                    details={key: event[key] for key in ("proposed_nodes", "review") if key in event})
        elif kind == "response_format_failed":
            add("construction", "response_format_failed", event["error"], index, event,
                details={"response": event["response"]})
        elif kind == "contract_error":
            add("construction", "delivery_contract_error", event["error"], index, event,
                details={key: event[key] for key in ("source", "invoke") if key in event})
        elif kind == "execution":
            implementations[tool] = event["source"]
            # Reaching execution establishes that this version passed the pre-execution contract.
            resolve(tool, {"delivery_contract_error"}, index)
            result = event["result"]
            details = {"arguments": event.get("specification", {}).get("example_arguments", {}),
                "invoke": event.get("upstream_example_invoke"),
                "result": result, "source": event["source"]}
            if result.get("error") is not None or result.get("status") != "completed":
                add("construction", "execution_failed", str(result.get("error") or result.get("status")), index, event, details=details)
            else:
                resolve(tool, {"execution_failed"}, index)
            if event.get("interface_error"):
                add("construction", "output_interface_error", event["interface_error"], index, event, details=details)
            elif result.get("error") is None and result.get("status") == "completed":
                resolve(tool, {"output_interface_error"}, index)
        elif kind == "judgment":
            if event["status"] == "Complete":
                # This resolves only the model's objection, never an observed execution/delivery failure.
                resolve(tool, {"native_judgment_incomplete"}, index)
                if event["score"] >= min_score:
                    resolve(tool, {"generality_below_threshold"}, index)
                else:
                    add("judgment", "generality_below_threshold", event["reasoning"], index, event,
                        source="model_assessment", details={"score": event["score"], "threshold": min_score,
                            "scope": event.get("score_scope", "construction_and_export"),
                            "blocks_candidate_export": event.get("score_scope") != "construction_library"})
            else:
                add("judgment", "native_judgment_incomplete", event["reasoning"], index, event,
                    source="model_assessment", details={"status": event["status"], "score": event["score"]})
        elif kind in {"repair_limit", "replan_limit", "graph_blocked"}:
            add("construction", kind, "Construction stopped: " + kind, index, event)
        elif kind == "export_failed":
            add("delivery", event.get("error_code", "export_adaptation_failed"), event["error"], index, event,
                details={key: event[key] for key in ("source", "invoke") if key in event})
        elif kind == "delivery_check":
            if event["passed"]:
                resolve(tool, {"delivery_failed"}, index)
                continue
            result = event.get("result") or {}
            messages = [event[key] for key in ("path_error", "interface_error", "acceptance_error") if event.get(key)]
            if result.get("error") is not None:
                messages.append(str(result["error"]))
            # Raw result includes success=false/error_message even when the worker completed normally.
            add("delivery", "delivery_failed", "; ".join(messages) or "Clean-workspace delivery did not complete successfully",
                index, event, details={"source": implementations.get(tool), **{key: event[key] for key in
                    ("arguments", "original_arguments", "path_bindings", "workspace", "result", "acceptance", "patch_check") if key in event}})
        elif kind == "export_verified":
            exported = event["tools"]

    for index, call in enumerate(calls):
        failure = call.get("error")
        if not failure:
            continue
        logical_id = call.get("logical_call_id")
        recovered = next((later for later in range(index + 1, len(calls))
            if logical_id and calls[later].get("logical_call_id") == logical_id
            and calls[later].get("response") and not calls[later].get("error")), None)
        issues.append(ConstructionIssue(stage="request", code=failure.get("code", "model_request_failed") if isinstance(failure, dict) else "model_request_failed",
            source="observation", message=str(failure.get("message", failure)) if isinstance(failure, dict) else str(failure),
            call_index=index, status="resolved" if recovered is not None else "unresolved", resolution_call_index=recovered,
            details={key: call[key] for key in ("purpose", "attempt", "logical_call_id", "recovery") if key in call}))
    if error:
        add("termination", error.code, error.message)
    elif status == "empty":
        add("termination", "no_tool_change" if exported else "no_exportable_tools",
            "No changed tool candidate was produced" if exported else "No final tool was exported",
            details={"selected_tools": next((e["tools"] for e in reversed(events) if e["kind"] == "export_selected"), []),
                "planned_tools": next(({name: {"description": node["description"], "deliverable": node["deliverable"],
                    "completed": node["completed"]} for name, node in e["value"]["nodes"].items()}
                    for e in reversed(events) if e["kind"] in {"plan_finished", "plan"}), {})})
    return ConstructionFeedback(operator_id="oscopilot_tool_evolver", outcome=status, issues=issues,
        exported_tools=exported, record_ref=record_ref)
