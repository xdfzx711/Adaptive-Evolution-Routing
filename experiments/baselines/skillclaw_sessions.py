"""Task-local SkillClaw launch context; independent of AER's skill candidates."""

import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile
import uuid

from plugins.skills.codex import repository_skill_overrides


def is_link(path):
    return path.is_symlink() or getattr(path, "is_junction", lambda: False)()


def inventory(root):
    """Digest complete bundles, including binary assets; reject indirect files."""
    root = Path(root).resolve(strict=True)
    files = {}
    for bundle in sorted(root.iterdir()):
        if is_link(bundle):
            raise ValueError("Skill libraries cannot contain links or junctions")
        if not bundle.is_dir() or not (bundle / "SKILL.md").is_file():
            continue
        for path in sorted(bundle.rglob("*")):
            if is_link(path) or not path.resolve().is_relative_to(root):
                raise ValueError("Skill bundles cannot contain links or junctions")
            if path.is_file():
                files[path.relative_to(root).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    digest = hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()
    return {"digest": digest, "files": files}


def copy_bundles(source, destination):
    expected = inventory(source)
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    for name in sorted({Path(path).parts[0] for path in expected["files"]}):
        shutil.copytree(Path(source) / name, destination / name)
    if inventory(destination) != expected:
        raise ValueError("Skill bundle copy did not preserve file contents")
    return expected


class NativeCodexLaunch:
    def __init__(self, client, library, *, credential_names=()):
        self.client, self.library = client, Path(library)
        self.credential_names = credential_names
        self.sessions = []

    def prepare(self, workspace, run_ctx):
        session = NativeCodexSession(self, workspace, run_ctx)
        self.sessions.append(session)
        return session


class NativeCodexSession:
    def __init__(self, adapter, workspace, run_ctx):
        self.adapter = adapter
        self.workspace = Path(workspace).resolve()
        self.session_id = "aer-" + uuid.uuid4().hex
        self.operation_id = run_ctx.operation_id
        self.disabled_repository_skills = repository_skill_overrides(self.workspace)
        self.root = Path(tempfile.mkdtemp(prefix=".aer-skillclaw-", dir=self.workspace))
        self.report = None
        self.failure = None
        self.finished = False
        try:
            self.version = copy_bundles(adapter.library, self.root)
            adapter.client.request("begin", {"session_id": self.session_id, "skills_dir": str(self.root)})
        except BaseException:
            self._remove()
            raise
        self.environment = dict(os.environ)
        for name in adapter.credential_names:
            self.environment.pop(name, None)
        self.environment["AER_SKILLCLAW_PROXY_KEY"] = adapter.client.proxy_key

    def arguments(self, args):
        args = list(args)
        for index, argument in enumerate(args):
            if argument.startswith("permissions.aer_task.filesystem={"):
                # CLI dotted-key parsing does not preserve quoted path segments.
                # Extend the original inline filesystem table instead.
                args[index] = argument[:-1] + ", " + json.dumps(str(self.root)) + '="read"}'
                break
        else:
            raise ValueError("Codex launch is missing the task filesystem table")
        provider = "model_providers.aer_skillclaw."
        overrides = [
            'model_provider="aer_skillclaw"', provider + 'name="SkillClaw native proxy"',
            provider + "base_url=" + json.dumps(self.adapter.client.url + "/v1"),
            provider + 'wire_api="responses"', provider + 'env_key="AER_SKILLCLAW_PROXY_KEY"',
            provider + 'requires_openai_auth=false', provider + 'supports_websockets=false',
            provider + 'http_headers={"X-Session-Id"=' + json.dumps(self.session_id) + '}',
            'features.skip_host_skill_discovery=true', 'features.plugins=false',
            self.disabled_repository_skills,
            'shell_environment_policy.exclude=["AER_SKILLCLAW_*", "*API_KEY*", "*TOKEN*"]',
        ]
        return [*args[:-1], *[value for entry in overrides for value in ("--config", entry)], args[-1]]

    def _remove(self):
        # Only delete the task-local directory created by this object.
        if self.root.parent != self.workspace or not self.root.name.startswith(".aer-skillclaw-"):
            raise ValueError("Invalid runtime cleanup target")
        if is_link(self.root) or self.root.resolve().parent != self.workspace:
            raise ValueError("Runtime cleanup target was replaced with a link")
        if self.root.exists():
            shutil.rmtree(self.root)

    def finish(self):
        if self.finished:
            if self.failure:
                raise RuntimeError(self.failure)
            return self.report
        try:
            unchanged = inventory(self.root) == self.version
            native = self.adapter.client.request("finish", {"session_id": self.session_id})
            for receipt in native.get("read_receipts", []):
                path = Path(receipt["path"]).resolve()
                relative = path.relative_to(self.root).as_posix()
                receipt["relative_path"] = relative
                receipt["matches_supplied_version"] = self.version["files"].get(relative) == receipt["sha256"]
                if not receipt["matches_supplied_version"]:
                    unchanged = False
            self.report = {"runtime": "skillclaw_native", "session_id": self.session_id,
                "library": self.version, "unchanged_during_execution": unchanged,
                "body_attribution": "verified_exposure_only_not_causal" if native.get("read_receipts") else "unverified",
                **native}
            if not unchanged:
                raise ValueError("Task modified the supplied SkillClaw library")
        except Exception as exc:
            self.failure = str(exc)
            raise
        finally:
            self.finished = True
            try:
                self._remove()
            except Exception as exc:
                self.failure = str(exc)
                raise
        return self.report
