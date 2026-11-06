import ast
from pathlib import Path
import warnings

from aer_core.config.models import PositiveFloat, Settings
from aer_core.errors import ExecutionError
from aer_core.schemas.workspace import CollectedResult
from plugins.agents.output import PublicJsonOutput
from .git import git
from .patches import patch_changes


def parse_python(source, filename="<source>"):
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", SyntaxWarning)
        return ast.parse(source, filename=filename)


class PatchCollectorSettings(Settings):
    git_timeout_seconds: PositiveFloat = 30.0


class JsonResultCollector:
    def __init__(self, adapter=None):
        self.adapter = adapter or PublicJsonOutput()

    def schema(self, task):
        return self.adapter.schema(task)

    def collect(self, task, workspace, final_text, run_ctx):
        return CollectedResult(output=self.adapter.parse(final_text))


class RawTextResultCollector:
    """Preserve the final answer exactly, including whitespace and code fences."""

    def schema(self, task):
        return None

    def collect(self, task, workspace, final_text, run_ctx):
        if not isinstance(final_text, str):
            raise ExecutionError("Raw text collector requires a final answer string", stage=run_ctx.stage)
        return CollectedResult(output={"response": final_text})


class GitPatchCollector:
    def __init__(self, settings, artifacts):
        self.settings, self.artifacts = settings, artifacts

    def schema(self, task):
        return None

    def collect(self, task, workspace, final_text, run_ctx):
        if workspace.baseline_revision is None:
            raise ExecutionError("GitPatchCollector requires a versioned workspace", stage=run_ctx.stage)
        root, timeout = workspace.path, self.settings.git_timeout_seconds
        # Intent-to-add makes new, nonignored files visible to the same diff.
        git(root, "add", "--intent-to-add", "--all", "--", ".", timeout=timeout)
        patch = git(root, "diff", "--binary", "--no-ext-diff", "--no-renames",
                    workspace.baseline_revision, "--", ".", timeout=timeout)
        try:
            _, files = patch_changes(patch)
            valid = True
        except ValueError:
            files, valid = set(), False
        checked, errors = 0, []
        for name in sorted(files):
            file = root / Path(name)
            if not file.is_file() or file.suffix != ".py":
                continue
            # This is parsing only: no imports, installation, or test execution.
            try:
                previous = git(root, "show", workspace.baseline_revision + ":" + name, timeout=timeout)
            except Exception:
                previous = None
            if previous is not None:
                try:
                    parse_python(previous)
                except SyntaxError:
                    continue  # Historical Python syntax cannot be checked here.
            checked += 1
            try:
                parse_python(file.read_bytes(), filename=name)
            except (SyntaxError, UnicodeError, ValueError):
                errors.append(name)
        ref = self.artifacts.put(patch.encode("utf-8"), "code_patch")
        return CollectedResult(output={"patch": patch, "changed_files": sorted(files),
            "base_commit": workspace.source_revision, "patch_valid": valid,
            "syntax_valid": not errors, "syntax_checked_files": checked, "syntax_error_files": errors},
            artifact_refs=[ref], state={"result_kind": "code_patch", "evaluation_mode": "static"})
