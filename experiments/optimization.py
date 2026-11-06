"""Compose isolated optimizer trials without an orchestrator or version store."""

from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory

from aer_core.errors import ContractError
from aer_core.feedback import PublicFeedbackBuilder
from aer_core.identifiers import timestamp_id
from aer_core.plugin_manager import PluginManager
from aer_core.schemas.feedback import ContextPolicy, TaskFeedbackPolicy
from aer_core.schemas.optimization import TrialObservation
from aer_core.schemas.plugins import PluginBinding
from aer_core.task_executor import ScoredTaskExecutor
from plugins.storage.artifacts import LocalArtifactStore
from .bootstrap import agent_bindings, component_binding, with_providers
from .environment import provider_dependencies, resolve_model


class ConfiguredModelFactory:
    """Build a separate provider from its own settings and environment names."""

    def __init__(self, loaded, registry):
        self.loaded, self.registry = loaded, registry

    def create(self, settings):
        data = self.loaded.config.model_dump(mode="json")
        data["providers"]["model"] = settings.model_dump(mode="json")
        loaded = resolve_model(replace(self.loaded, config=type(self.loaded.config).model_validate(data)), required=True)
        entry = self.registry.get("model_provider", settings.plugin)
        aliases = {"settings": "model_settings", "credentials": "model_credentials", "tool_provider": "tools"}
        bindings = with_providers(loaded.config, self.registry, [PluginBinding(name="model",
            category="model_provider", plugin_id=settings.plugin, config=settings.config,
            dependency_bindings={key: aliases.get(key, key) for key in entry.metadata.dependencies})])
        return PluginManager(self.registry).build(bindings,
            dependencies=provider_dependencies(loaded, bindings)).instances["model"]


class IsolatedCandidateTrials:
    """Fresh plugin graph and temporary content store for every trial.

    The only external store capability used here is reading Snapshot artifacts.
    No outer Agent, Evaluator instance, validation cache or stable pointer is used.
    """

    def __init__(self, loaded, registry, artifacts, tasks, *, snapshot_loader=None, evaluator_settings=None):
        self.loaded, self.registry, self.artifacts = loaded, registry, artifacts
        self.snapshot_loader = snapshot_loader
        self.evaluator_settings = evaluator_settings
        self.available = {task.task_id: task for task in tasks} if loaded.config.benchmark.execution_split == "dev" else {}

    def with_evaluator(self, settings):
        """Clone the trial composition with an explicit scorer; keep task/Agent inputs."""
        entry = self.registry.get("evaluator", settings.plugin)
        entry.config_model.model_validate(settings.config)
        return type(self)(self.loaded, self.registry, self.artifacts, tuple(self.available.values()),
            snapshot_loader=self.snapshot_loader, evaluator_settings=settings)

    def tasks(self, current_task, task_ids):
        current = self.available.get(current_task.task_id)
        if current is None or current.identity != current_task:
            raise ContractError("Optimizer current task must be a selected development task", stage="evolution")
        ids = task_ids if task_ids is not None else (current_task.task_id,)
        if not ids or len(set(ids)) != len(ids) or any(name not in self.available for name in ids):
            raise ContractError("Optimizer tasks must be unique selected development tasks", stage="evolution")
        if self.loaded.config.state.task_mode == "independent" and set(ids) != {current_task.task_id}:
            raise ContractError("Independent task mode restricts optimizer trials to the current task", stage="evolution")
        return tuple(self.available[name].identity for name in ids)

    def evaluate(self, current_task, task, snapshot, prompt, run_ctx):
        self.tasks(current_task, (task.task_id,))
        source = self.available[task.task_id]
        if source.identity != task or not prompt.strip() or snapshot.agent_plugin_id != self.loaded.config.agent.plugin:
            raise ContractError("Optimizer trial task, prompt or Agent is invalid", stage="evolution")
        # EvolutionContext intentionally contains only the disclosed config view.
        # Rebuild from the exact frozen version through a read-only loader, never
        # from that filtered view or from a mutable stable pointer.
        full = self.snapshot_loader(snapshot.version_id) if self.snapshot_loader is not None else snapshot
        public_config, full_config = (item.model_dump(mode="json")["config"] for item in (snapshot, full))
        if full.version_id != snapshot.version_id or full.agent_plugin_id != snapshot.agent_plugin_id or any(
                full.artifacts.get(kind) != ref for kind, ref in snapshot.artifacts.items()) or any(
                full_config.get(key) != value for key, value in public_config.items()):
            raise ContractError("Optimizer Snapshot view differs from its frozen version", stage="evolution")
        snapshot = full
        config = self.loaded.config
        with TemporaryDirectory(prefix=timestamp_id("aer-trial") + "-") as directory:
            root = Path(directory)
            store = LocalArtifactStore(root)
            for ref in snapshot.artifacts.values():
                store.import_ref(ref, self.artifacts.get(ref))
            updates = dict(snapshot.artifacts)
            updates["prompt"] = store.put(prompt.encode("utf-8"), "prompt")
            trial = snapshot.replace(version_id=timestamp_id("trial-version"), parent_version_id=snapshot.version_id,
                                     artifacts=updates)
            bindings = with_providers(config, self.registry, [*agent_bindings(config, self.registry),
                component_binding("benchmark", "benchmark", config.benchmark.plugin, config.benchmark.config, self.registry),
                component_binding("evaluator", "evaluator",
                    self.evaluator_settings.plugin if self.evaluator_settings else config.assessment.evaluator,
                    self.evaluator_settings.config if self.evaluator_settings else config.assessment.evaluator_config, self.registry)])
            dependencies = provider_dependencies(self.loaded, bindings)
            dependencies.update(initial_snapshot=trial, artifact_store=store)
            built = PluginManager(self.registry).build(bindings, dependencies=dependencies)
            agent, benchmark, evaluator = (built.instances[name] for name in ("agent", "benchmark", "evaluator"))
            from .native_scoring import validate_native_scoring
            validate_native_scoring(benchmark, config, self.registry, self.evaluator_settings)
            from aer_core.scoring import configure_scoring
            evaluator = configure_scoring(evaluator, config.assessment.scoring)
            declaration = benchmark.feedback_policy(source)
            if not isinstance(declaration, TaskFeedbackPolicy) or declaration.task != task or not declaration.allow_evolution:
                raise ContractError("Optimizer task does not permit public evolution feedback", stage="evolution")
            capabilities = agent.capabilities()
            tools = built.instances.get("tools")
            specs = tools.specifications() if tools is not None else ()
            from aer_core.scoring_feedback import scorer_feedback_policy
            policy = ContextPolicy(task=task, output_fields=declaration.output_fields,
                public_check_ids=declaration.public_check_ids,
                **scorer_feedback_policy(evaluator, self.evaluator_settings.plugin if self.evaluator_settings else config.assessment.evaluator),
                metadata_fields=declaration.metadata_fields, allow_evolution=True,
                reason_messages={**declaration.reason_messages, **getattr(evaluator, "public_reason_messages", {})},
                state_fields=capabilities.feedback_state_fields,
                tool_arguments={**dict(capabilities.feedback_tool_arguments), **{tool.name: tool.feedback_arguments for tool in specs}},
                tool_outputs={**dict(capabilities.feedback_tool_outputs), **{tool.name: tool.feedback_outputs for tool in specs}})
            context = replace(run_ctx, run_id=timestamp_id("trial-run"), episode_id=timestamp_id("trial-episode"),
                operation_id=timestamp_id("trial"), stage="evolution", budget=None, workspace_ref=str(root),
                execution_workspace_root=str(root / "workspaces"))
            result = ScoredTaskExecutor(agent, benchmark, evaluator).execute(source, trial, context)
            public = benchmark.task_view(source)
            # Filter task metadata even when execution did not produce scoreable evidence.
            public = public.replace(public_metadata={name: value for name, value in public.public_metadata.items()
                                                     if name in declaration.metadata_fields})
            if result.error is not None or not result.complete:
                error = result.error or ContractError("Optimizer trial reported incomplete usage", stage="evolution").to_info()
                return TrialObservation(task=public, execution=None, feedback=None, usage=result.usage,
                    error=error.replace(message="Internal task execution or scoring failed; code=" + error.code, evidence_ref=None))
            public, execution, feedback = PublicFeedbackBuilder(policy).public_views(public, result.execution, result.evaluation)
            return TrialObservation(task=public, execution=execution, feedback=feedback, usage=result.usage,
                                    score_details=result.evaluation.score_details)
