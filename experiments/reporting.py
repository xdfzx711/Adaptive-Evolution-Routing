"""One evolution.txt per run and compact, independently controlled console progress."""

import sys
from datetime import datetime
from pathlib import Path
from typing import TextIO

from aer_core.progress import report_progress
from .evolution_report import EvolutionTextReport
from .round_report import RoundReport


class RunReporter:
    def __init__(self, run_root: Path, *, console: bool = False, stream: TextIO | None = None):
        self.path = run_root / "evolution.txt"
        self.console = console
        self.stream = stream if stream is not None else sys.stderr
        self.report = None
        self._file_failed = False

    def configure(self, task, config, context, artifacts, versions):
        self.task, self.config, self.context = task, config, context
        self.report = EvolutionTextReport(self._write, artifacts, versions, root=self.path.parent)
        self.round_report = RoundReport(self.path.parent, task, context, artifacts, config=config)
        self.report.readable_sources = self.round_report.readable_sources

    def _write(self, text):
        if self._file_failed:
            return
        try:
            with self.path.open("a", encoding="utf-8") as output:
                output.write(text)
        except OSError:
            self._file_failed = True
            print(f"无法写入进化报告：{self.path}", file=self.stream, flush=True)

    def record(self, kind, value, ref=None):
        if self.report is None:
            return
        try:
            self.round_report.record(kind, value, ref)
        except Exception as error:
            self.emit("报告异常", f"{kind} 未能写入分轮记录：{type(error).__name__}")
        if kind == "episode_start":
            self.report.begin(self.task, value, self.config, self.context)
        else:
            self.report.record(kind, value, ref)
        if kind == "evolution_proposal" and self.round_report.path:
            self.emit("记录", f"第 {value.round_id} 轮记录：{self.round_report.path}")

    def emit(self, stage: str, message: str, *, detail: bool = False):
        now = datetime.now().astimezone()
        message = " ".join(str(message).split())
        line = f"[{now.strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]} {now.strftime('%z')}] [{stage}] {message}\n"
        if not detail and (stage in {"进化", "验证", "报告异常"} or stage == "生成" and "正在" in message):
            self._write(line)
        if self.console:
            self.stream.write(line)
            self.stream.flush()


class ReportingTaskExecutor:
    """Decorate execution without changing paired-validation cache conditions."""

    def __init__(self, executor, reporter):
        self.executor, self.reporter = executor, reporter

    def execute(self, task, snapshot, run_ctx):
        side = "旧版" if "/baseline/" in run_ctx.operation_id else "候选"
        group = {"local_validation": "当前任务", "similar_validation": "同类", "regression_validation": "回归"}[run_ctx.stage]
        report_progress(self.reporter, "验证进度", f"{side} / {group} / {task.task_id}：执行中", detail=True)
        result = self.executor.execute(task, snapshot, run_ctx)
        status = (f"异常 {result.error.code}" if result.error else
            f"{'通过' if result.evaluation.success else '未通过'}，得分={result.evaluation.score:.3f}")
        report_progress(self.reporter, "验证进度", f"{side} / {group} / {task.task_id}：{status}", detail=True)
        return result


def summarize_saved_run(path: Path) -> Path:
    from .report_export import export_saved_run
    return export_saved_run(path)
