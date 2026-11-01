"""Prepare a private development workspace from the exact frozen runtime settings."""

from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

from aer_core.errors import ContractError
from aer_core.plugin_manager import PluginManager
from aer_core.schemas.feedback import TaskFeedbackPolicy
from plugins.tools.catalog import ToolingConfig


class ConfiguredEvolutionWorkspaces:
    def __init__(self, loaded, registry, tasks=(), *, snapshot_loader=None):
        self.loaded, self.registry, self.snapshot_loader = loaded, registry, snapshot_loader
        self.tasks = {task.task_id: task for task in tasks} if loaded.config.benchmark.execution_split == "dev" else {}

    @contextmanager
    def open(self, task, snapshot, run_ctx):
        from .bootstrap import component_binding
        from .environment import provider_dependencies

        source = self.tasks.get(task.task_id)
        if source is None or source.identity.benchmark_id != task.benchmark_id:
            raise ContractError("Tool construction requires the current selected development task", stage="evolution")
        full = self.snapshot_loader(snapshot.version_id) if self.snapshot_loader else snapshot
        public_config, full_config = (item.model_dump(mode="json")["config"] for item in (snapshot, full))
        if (full.version_id != snapshot.version_id or full.agent_plugin_id != snapshot.agent_plugin_id
            or full.artifacts != snapshot.artifacts or any(full_config.get(key) != value for key, value in public_config.items())):
            raise ContractError("Construction Snapshot differs from its frozen version", stage="evolution")
        tooling = ToolingConfig.model_validate(full_config.get("tooling"))
        if not tooling.generated_tools or not tooling.generated_tools.enabled:
            raise ContractError("Generated tool execution is not enabled", stage="evolution")
        config = self.loaded.config
        selection = config.agent.config.get("workspace", {"plugin": "empty_workspace", "config": {}})
        bindings = [component_binding("workspace", "workspace_provider", selection["plugin"], selection.get("config", {}), self.registry),
            component_binding("benchmark", "benchmark", config.benchmark.plugin, config.benchmark.config, self.registry)]
        built = PluginManager(self.registry).build(bindings, dependencies=provider_dependencies(self.loaded, bindings))
        benchmark = built.instances["benchmark"]
        policy = benchmark.feedback_policy(source)
        if not isinstance(policy, TaskFeedbackPolicy) or not policy.allow_evolution:
            raise ContractError("Development task does not permit evolution", stage="evolution")
        provider = built.instances["workspace"]
        with TemporaryDirectory(prefix="aer-tool-construction-") as directory:
            root = Path(directory)
            context = replace(run_ctx, execution_workspace_root=None, workspace_ref=str(root))
            handle = provider.prepare(task, root / "task", context)
            try:
                yield SimpleNamespace(path=handle.path, repository=root / "repository", root=root, tooling=tooling)
            finally:
                provider.release(handle)
