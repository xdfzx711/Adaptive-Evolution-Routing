"""Export only a task's base tree, with a fresh local history and no remote."""

import io
import os
from pathlib import Path
import re
import tarfile
from pydantic import Field

from aer_core.config.models import NonBlank, PositiveFloat, Settings
from aer_core.errors import ExecutionError
from aer_core.schemas.workspace import WorkspaceHandle
from .git import git


def export_tree(archive: bytes, directory: Path) -> bool:
    """Export source files and preserve links to regular files inside this tree."""
    root = directory.resolve()
    links = []
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:") as tree:
        for member in tree:
            target = (root / member.name).resolve()
            if not target.is_relative_to(root):
                raise ExecutionError("Repository archive path escapes the workspace", stage="initial_execution")
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
            elif member.isfile():
                target.parent.mkdir(parents=True, exist_ok=True)
                stream = tree.extractfile(member)
                if stream is not None:
                    target.write_bytes(stream.read())
            elif member.issym():
                link = Path(member.linkname)
                if link.is_absolute() or link.drive or not (target.parent / link).resolve().is_relative_to(root):
                    raise ExecutionError("Repository symlink target escapes the workspace: " + member.name,
                                         stage="initial_execution")
                links.append((target, member.linkname))
            else:
                raise ExecutionError("Repository archive contains an unsupported entry: " + member.name,
                                     stage="initial_execution")
    # Targets may occur later in Git's archive order. Create links only after
    # regular files have been exported, preserving the original relative link.
    for target, linkname in links:
        source = (target.parent / linkname).resolve()
        if not source.is_relative_to(root) or not source.is_file():
            raise ExecutionError("Repository symlink must target a regular file in the workspace: " + str(target),
                                 stage="initial_execution")
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.symlink(str(Path(linkname)), target)
        except OSError as error:
            raise ExecutionError("Cannot create repository symlink: " + str(target) + "; " + str(error),
                                 stage="initial_execution") from error
    return bool(links)


class LocalRepoSettings(Settings):
    cache_root: NonBlank = "../../.aer-cache/repos"
    repositories: dict[str, NonBlank] = Field(default_factory=dict)
    git_timeout_seconds: PositiveFloat = 120.0


class EmptyWorkspace:
    def prepare(self, task, directory, run_ctx):
        directory.mkdir(parents=True, exist_ok=True)
        return WorkspaceHandle(path=directory)

    def release(self, workspace):
        pass


class LocalRepoWorkspace(EmptyWorkspace):
    def __init__(self, settings: LocalRepoSettings, config_root: Path):
        self.settings = settings
        root = Path(settings.cache_root).expanduser()
        self.cache_root = (config_root / root).resolve() if not root.is_absolute() else root.resolve()
        self.config_root = config_root

    def prepare(self, task, directory, run_ctx):
        repo, revision = task.input.get("repo"), task.input.get("base_commit")
        if (not isinstance(repo, str) or not re.fullmatch(r"[\w.-]+/[\w.-]+", repo)
                or any(part in {".", ".."} for part in repo.split("/"))
                or not isinstance(revision, str) or not re.fullmatch(r"[0-9a-f]{40}", revision)):
            raise ExecutionError("Repository workspace requires public repo and full base_commit", stage="initial_execution")
        cache_name = repo.replace("/", "__") + "-" + revision
        source_cache = self.cache_root / cache_name
        cache = (Path(run_ctx.execution_workspace_root).parent / "repository-cache" / cache_name
                 if run_ctx.execution_workspace_root else source_cache)
        cache.mkdir(parents=True, exist_ok=True)
        timeout = self.settings.git_timeout_seconds
        git(cache, "init", "--quiet", timeout=timeout)
        try:
            git(cache, "cat-file", "-e", revision + "^{commit}", timeout=timeout)
        except ExecutionError:
            source = (str(source_cache) if run_ctx.execution_workspace_root and (source_cache / ".git").is_dir()
                      else self.settings.repositories.get(repo, "https://github.com/" + repo + ".git"))
            if not source.startswith(("https://", "http://", "ssh://", "git@")):
                path = Path(source).expanduser()
                source = str((self.config_root / path).resolve() if not path.is_absolute() else path.resolve())
            git(cache, "fetch", "--quiet", "--depth=1", "--no-tags", source, revision, timeout=timeout)
        archive = git(cache, "archive", "--format=tar", revision, timeout=timeout, binary=True)
        directory.mkdir(parents=True, exist_ok=True)
        # No external object store, later commit, reference answer, or scoring
        # test is copied. Original in-tree file symlinks are retained.
        has_links = export_tree(archive, directory)
        git(directory, "init", "--quiet", timeout=timeout)
        if has_links:
            git(directory, "config", "core.symlinks", "true", timeout=timeout)
        git(directory, "add", "--all", "--force", timeout=timeout)
        git(directory, "-c", "user.name=AER", "-c", "user.email=aer@localhost",
            "commit", "--quiet", "--allow-empty", "-m", "Task base tree", timeout=timeout)
        baseline = git(directory, "rev-parse", "HEAD", timeout=timeout).strip()
        return WorkspaceHandle(path=directory, baseline_revision=baseline, source_revision=revision)
