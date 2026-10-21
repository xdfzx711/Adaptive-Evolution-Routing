"""Aggregate actual operation costs without replacing unknown tokens with zero."""

from aer_core.schemas.usage import ResourceUsage


def sum_usage(reports) -> ResourceUsage:
    reports = tuple(reports)
    def tokens(field):
        values = [getattr(report, field) for report in reports]
        return None if any(value is None for value in values) else sum(values)
    incoming, outgoing = tokens("input_tokens"), tokens("output_tokens")
    return ResourceUsage(input_tokens=incoming, output_tokens=outgoing,
        total_tokens=None if incoming is None or outgoing is None else incoming + outgoing,
        model_calls=tokens("model_calls"), tool_calls=sum(item.tool_calls for item in reports),
        elapsed_seconds=sum(item.elapsed_seconds for item in reports),
        usage_complete=all(item.usage_complete for item in reports),
        usage_sources=list(dict.fromkeys(source for item in reports for source in item.usage_sources)),
        pending_reservations=[reservation for item in reports for reservation in item.pending_reservations])
