"""Optional progress output independent of storage, CLI and model SDKs."""

from typing import Protocol, runtime_checkable


@runtime_checkable
class ProgressReporter(Protocol):
    def emit(self, stage: str, message: str, *, detail: bool = False) -> None: ...


class NullProgressReporter:
    def emit(self, stage: str, message: str, *, detail: bool = False) -> None:
        pass


def report_progress(reporter: ProgressReporter | None, stage: str, message: str, *, detail: bool = False) -> None:
    if reporter is not None:
        try:
            reporter.emit(stage, message, detail=detail)
        except Exception:
            # Observability must not change an evolution decision or its commit result.
            pass


def report_record(reporter, kind: str, value, ref=None) -> None:
    """Send saved domain records to an optional report renderer, never back to agents."""
    record = getattr(reporter, "record", None)
    if callable(record):
        try:
            record(kind, value, ref)
        except Exception as error:
            report_progress(reporter, "报告异常", f"{kind} 未能写入阅读报告：{type(error).__name__}")
