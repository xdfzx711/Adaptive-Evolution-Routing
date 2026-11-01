"""Write-ahead MCP invocation receipts and totals independent of the retained tail."""

from datetime import datetime, timezone
from hashlib import sha256
import json
from uuid import uuid4


def digest(value):
    return sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()


def empty_counts(kind):
    return {"kind": kind, "attempted": 0, "completed": 0, "execution_success": 0, "execution_error": 0,
        "business_success": 0, "business_failure": 0, "business_unknown": 0, "business_not_evaluated": 0,
        "elapsed_seconds": 0.0}


def begin(report, name, kind, arguments, identity=None):
    counts = report.setdefault("tool_metrics", {}).setdefault(name, empty_counts(kind))
    counts["attempted"] += 1
    entry = {"invocation_id": uuid4().hex, "tool": name, "kind": kind, "status": "started",
        "started_at": datetime.now(timezone.utc).isoformat(), "implementation_digest": identity,
        "business_status": "not_evaluated", "elapsed_seconds": None}
    if kind == "generated":
        entry["arguments_digest"] = digest(arguments)
    report["invocation_count"] += 1
    tail = [*report["invocations"], entry]
    report["invocations_omitted"] = report.get("invocations_omitted", 0) + max(0, len(tail) - 100)
    report["invocations"] = tail[-100:]
    return entry


def finish(report, entry, *, error, elapsed, outcome=None, result=None):
    entry.update(status="error" if error else "completed", elapsed_seconds=elapsed,
        finished_at=datetime.now(timezone.utc).isoformat(), **(outcome or {"business_status": "not_evaluated" if error else "unknown"}))
    if entry["kind"] == "generated" and result is not None:
        entry["output_digest"] = digest(result)
    counts = report["tool_metrics"][entry["tool"]]
    counts["completed"] += 1
    counts["execution_error" if error else "execution_success"] += 1
    counts["business_" + entry["business_status"]] += 1
    counts["elapsed_seconds"] += elapsed
