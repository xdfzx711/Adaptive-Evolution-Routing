"""Reconstruct a patch's source context without changing Agent collection or inputs."""

from dataclasses import replace
from pathlib import Path
import subprocess
import tempfile

from aer_core.errors import ExecutionError
from aer_core.schemas.task import TaskView
from plugins.workspace.git import git


class InvalidCandidatePatch(ValueError):
    pass


def source_context(task, patch, paths, workspace_provider, run_ctx, *, max_chars, git_timeout):
    with tempfile.TemporaryDirectory(prefix="aer-patch-review-") as directory:
        root = Path(directory)
        handle = None
        try:
            public = TaskView(task_id=task.task_id, benchmark_id=task.benchmark_id,
                input=task.input, public_metadata={})
            # Review workspaces use the configured base-tree cache, never an Agent's mutable directory.
            handle = workspace_provider.prepare(public, root / "source", replace(run_ctx, execution_workspace_root=None))
            tree = handle.path.resolve()
            if handle.baseline_revision is None or handle.source_revision != task.input.get("base_commit"):
                raise ValueError("Review requires the exact task base revision")

            def read(name):
                path = tree / name
                if not path.resolve().is_relative_to(tree):
                    raise ValueError("Review source escapes its base tree")
                if not path.exists():
                    return None
                if not path.is_file() or path.stat().st_size > max_chars:
                    raise ValueError("Review source exceeds the configured context limit")
                return path.read_text(encoding="utf-8")

            before = {name: read(name) for name in paths}
            patch_path = root / "candidate.patch"
            patch_path.write_bytes(patch.encode("utf-8"))
            try:
                git(tree, "apply", "--check", "--", str(patch_path), timeout=git_timeout)
            except ExecutionError as failure:
                if isinstance(failure.__cause__, (OSError, subprocess.TimeoutExpired)):
                    raise
                raise InvalidCandidatePatch("Candidate patch cannot apply to the task base") from failure
            git(tree, "apply", "--", str(patch_path), timeout=git_timeout)
            files = [{"path": name, "before": before[name], "after": read(name)} for name in paths]
            if sum(len(item[side] or "") for item in files for side in ("before", "after")) > max_chars:
                raise ValueError("Review source exceeds the configured context limit")
            return files
        finally:
            if handle is not None:
                workspace_provider.release(handle)
