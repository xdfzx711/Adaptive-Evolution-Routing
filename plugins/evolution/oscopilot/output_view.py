"""Small model-facing views; full execution receipts remain unchanged."""

import json
import re


def bounded_value(value, limit):
    text = json.dumps(value, ensure_ascii=False)
    if len(text) <= limit:
        return value
    return {"truncated": True, "original_chars": len(text), "value_type": type(value).__name__,
            "keys": [str(key)[:100] for key in list(value)[:8]] if isinstance(value, dict) else None,
            "preview": text[:limit // 2], "note": "Partial JSON text, not the complete value."}


def execution_view(result, *, limit, full_output_ref):
    value = result.get("result")
    stdout = result.get("stdout") or ""
    # Native execution prints the return value; generated code may print it again.
    # Remove exact copies only, preserving unrelated diagnostics.
    if value is not None:
        copies = {str(value), repr(value), json.dumps(value, ensure_ascii=False)}
        for copy in sorted(copies, key=len, reverse=True):
            if copy:
                stdout = re.sub(r"(?m)^(?:<return>\r?\n)?" + re.escape(copy)
                                + r"(?:\r?\n</return>)?\r?$", "", stdout)
    view = {"status": result.get("status"), "result": bounded_value(value, limit),
            "error": bounded_value(result.get("error"), limit // 4),
            "full_output_ref": full_output_ref}
    if stdout.strip():
        view["stdout"] = bounded_value(stdout.strip(), limit // 4)
    return view
