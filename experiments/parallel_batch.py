"""Bounded task-level process isolation; each task retains sequential AER/GEPA rounds."""

from concurrent.futures import FIRST_COMPLETED, Future, ProcessPoolExecutor, wait
from concurrent.futures.process import BrokenProcessPool
from dataclasses import replace
from datetime import datetime, timezone
import multiprocessing
import os
from pathlib import Path
import sys

from aer_core.schemas.usage import ResourceUsage
from aer_core.errors import AERError
from plugins.storage._files import FileRoot, readable_json


def _error_entry(task_id, root, error):
    info = error.to_info() if isinstance(error, AERError) else None
    usage = info.usage if info is not None else None
    if usage is None:
        usage = ResourceUsage(input_tokens=None, output_tokens=None, total_tokens=None,
            model_calls=None, tool_calls=0, elapsed_seconds=0, usage_complete=False,
            usage_sources=["unknown"])
    detail = info.model_dump(mode="json") if info is not None else {"message": str(error)}
    if error.__cause__ is not None:
        detail["cause"] = str(error.__cause__)
    return {"task_id": task_id, "status": "error", "run_root": str(root),
            "final_evaluation": None, "error": {**detail, "type": type(error).__name__},
            "usage": usage.model_dump(mode="json")}


def _run_child(loaded, task_id, run_id, console, report_holdouts):
    # Import inside the spawn worker; no shared agents, version stores or caches.
    from experiments.batch import run_benchmark

    # Spawn does not execute CLI.main's UTF-8 stream setup.
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(encoding="utf-8")
    started = datetime.now(timezone.utc).isoformat()
    root = Path(loaded.config.storage.root) / run_id
    try:
        result = run_benchmark(loaded, run_id=run_id, console=console, report_holdouts=report_holdouts)
        entry, = result["tasks"]
        if entry["task_id"] != task_id:
            raise ValueError("worker returned a different task")
    except Exception as error:
        # AER/ConfigError constructors cannot be reconstructed from Exception.args.
        # Returning plain data prevents an unpickle failure from breaking the pool.
        entry = _error_entry(task_id, root, error)
    return {**entry, "run_root": str(root), "worker_pid": os.getpid(),
            "started_at": started, "finished_at": datetime.now(timezone.utc).isoformat()}


def run_parallel_tasks(loaded, tasks, root, *, workers, console, report_holdouts):
    """Yield completed entries promptly; queue at most workers tasks at a time.

    All references in an entry are relative to its run_root. The parent is the
    sole writer of the aggregate summary; workers only write their own run.
    """
    pending = iter(enumerate(tasks))
    with ProcessPoolExecutor(max_workers=min(workers, len(tasks)),
                             mp_context=multiprocessing.get_context("spawn")) as pool:
        futures = {}

        def submit_next():
            item = next(pending, None)
            if item is None:
                return
            index, task = item
            run_id = f"task-{index + 1:04d}"
            config = loaded.config.model_copy(update={
                "benchmark": loaded.config.benchmark.model_copy(update={"task_ids": [task.task_id]}),
                "storage": loaded.config.storage.model_copy(update={"root": str(root / "tasks")})})
            child = replace(loaded, config=config)
            try:
                future = pool.submit(_run_child, child, task.task_id, run_id, console, report_holdouts)
            except BrokenProcessPool as error:
                # A terminated process can break the pool. Still account for every
                # requested task and keep the aggregate readable/completed-with-errors.
                future = Future()
                future.set_exception(error)
            futures[future] = (task.task_id, root / "tasks" / run_id)

        for _ in range(min(workers, len(tasks))):
            submit_next()
        while futures:
            done, _ = wait(futures, return_when=FIRST_COMPLETED)
            for future in done:
                task_id, child_root = futures.pop(future)
                try:
                    entry = future.result()
                except Exception as error:
                    entry = _error_entry(task_id, child_root, error)
                if entry.get("error"):
                    files = FileRoot(child_root)
                    with files.locked():
                        files.replace("worker_error.json", readable_json(entry["error"]))
                if console:
                    detail = entry.get("error") or {}
                    reason = f" | {detail.get('type', detail.get('code', 'error'))}: {detail.get('message', '')}" if detail else ""
                    print(f"[batch] {task_id}: {entry['status']} ({child_root}){reason}", flush=True)
                yield entry
                submit_next()
