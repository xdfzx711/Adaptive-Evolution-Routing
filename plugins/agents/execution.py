"""Trace and usage bookkeeping for one synchronous Agent session."""

from datetime import datetime, timezone
from time import monotonic

from aer_core.schemas.execution import TraceEvent
from aer_core.schemas.usage import ResourceUsage, UsageReport


class SessionRecord:
    def __init__(self):
        self.start = monotonic()
        self.events = []
        self.reports = []

    def event(self, kind: str, payload: dict):
        self.events.append(TraceEvent(seq=len(self.events), kind=kind, payload=payload,
                                      timestamp=datetime.now(timezone.utc)))

    def add_usage(self, usage: UsageReport | ResourceUsage):
        self.reports.append(usage)

    def unknown_call(self, *, model: bool):
        self.add_usage(UsageReport(input_tokens=None if model else 0, output_tokens=None if model else 0,
            model_calls=int(model), tool_calls=int(not model), elapsed_seconds=0, usage_source="unknown"))

    def usage(self) -> ResourceUsage:
        def tokens(field):
            values = [getattr(report, field) for report in self.reports]
            return None if any(value is None for value in values) else sum(values)

        input_tokens, output_tokens = tokens("input_tokens"), tokens("output_tokens")
        sources = []
        pending = []
        complete = input_tokens is not None and output_tokens is not None
        for report in self.reports:
            reported_sources = report.usage_sources if isinstance(report, ResourceUsage) else (report.usage_source,)
            for source in reported_sources:
                if source not in sources:
                    sources.append(source)
            if isinstance(report, ResourceUsage):
                pending.extend(report.pending_reservations)
                complete = complete and report.usage_complete
        return ResourceUsage(input_tokens=input_tokens, output_tokens=output_tokens,
            total_tokens=None if input_tokens is None or output_tokens is None else input_tokens + output_tokens,
            model_calls=tokens("model_calls"),
            tool_calls=sum(report.tool_calls for report in self.reports), elapsed_seconds=monotonic() - self.start,
            usage_complete=complete and "unknown" not in sources and not pending,
            usage_sources=sources, pending_reservations=pending)
