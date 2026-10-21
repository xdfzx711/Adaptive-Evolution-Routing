"""Cross-field rules and declared plugin compatibility checks."""

from pydantic import ValidationError

from aer_core.errors import ConfigIssue

from .catalog import ConfigCatalog, PluginDeclaration
from .models import ExperimentConfig


def field_path(location: tuple[str | int, ...]) -> str:
    result = ""
    for part in location:
        result += f"[{part}]" if isinstance(part, int) else ("." if result else "") + part
    return result or "$"


def schema_issues(error: ValidationError, prefix: str = "") -> list[ConfigIssue]:
    issues = []
    for item in error.errors(include_url=False, include_input=False):
        field = field_path(item["loc"])
        issues.append(ConfigIssue(f"{prefix}.{field}" if prefix else field, item["msg"]))
    return issues


def validate_semantics(config: ExperimentConfig, catalog: ConfigCatalog) -> list[ConfigIssue]:
    issues: list[ConfigIssue] = []

    def require(condition: bool, field: str, message: str) -> None:
        if not condition:
            issues.append(ConfigIssue(field, message))

    def reference(category: str, name: str, field: str) -> PluginDeclaration | None:
        declaration = catalog.get(category, name)
        if declaration is None:
            known = ", ".join(catalog.names(category)) or "(none)"
            issues.append(ConfigIssue(field, f"unknown {category} plugin {name!r}; declared: {known}"))
        return declaration

    reference("runner", config.experiment.runner, "experiment.runner")
    benchmark = reference("benchmark", config.benchmark.plugin, "benchmark.plugin")
    agent = reference("agent", config.agent.plugin, "agent.plugin")
    model = reference("model_provider", config.providers.model.plugin, "providers.model.plugin")
    tools = reference("tool_provider", config.providers.tools.plugin, "providers.tools.plugin")
    reference("evaluator", config.assessment.evaluator, "assessment.evaluator")
    reference("diagnoser", config.assessment.diagnoser, "assessment.diagnoser")
    reference("validation", config.validation.plugin, "validation.plugin")
    policy = reference("acceptance_policy", config.validation.acceptance_policy, "validation.acceptance_policy")
    reference("experience_store", config.storage.experience, "storage.experience")
    reference("artifact_store", config.storage.artifacts, "storage.artifacts")
    reference("version_store", config.storage.versions, "storage.versions")
    router = None
    if config.routing.plugin is not None:
        router = reference("router", config.routing.plugin, "routing.plugin")

    if benchmark:
        require(config.benchmark.execution_split in benchmark.splits, "benchmark.execution_split", "split is not declared by the Benchmark")
        require(config.validation.split in benchmark.splits, "validation.split", "split is not declared by the Benchmark")
    if policy:
        require(config.validation.policy_version in policy.policy_versions, "validation.policy_version", f"supported policy versions: {', '.join(policy.policy_versions)}")
    if agent and agent.config_model:
        try:
            agent.config_model.model_validate(config.agent.model_dump(mode="json"))
        except ValidationError as error:
            issues.extend(schema_issues(error, "agent"))
    if model and model.config_model:
        try:
            model.config_model.model_validate(config.providers.model.config)
        except ValidationError as error:
            issues.extend(schema_issues(error, "providers.model.config"))
    if tools:
        for index, name in enumerate(config.providers.tools.enabled):
            require(name in tools.tools, f"providers.tools.enabled[{index}]", f"unknown tool {name!r}; declared: {', '.join(tools.tools)}")

    mode = config.evolution.mode
    rounds = config.limits.max_evolution_rounds
    routing = config.routing
    targets = config.targets
    require((config.agent.initial_prompt is None) != (config.agent.initial_snapshot_ref is None), "agent.initial_prompt", "provide exactly one of initial_prompt and initial_snapshot_ref")
    if config.agent.initial_snapshot_ref is not None:
        require(mode == "disabled", "agent.initial_snapshot_ref", "v0.1 frozen snapshots require evolution.mode=disabled")
    if agent and agent.fixed_max_steps is not None:
        require(config.agent.max_steps == agent.fixed_max_steps, "agent.max_steps", f"{agent.plugin_id} requires max_steps={agent.fixed_max_steps}")

    require(config.limits.max_output_tokens_per_call <= config.limits.max_tokens, "limits.max_output_tokens_per_call", "must not exceed limits.max_tokens")
    require(config.validation.similar_group != config.validation.regression_group, "validation.regression_group", "must differ from validation.similar_group")
    require(config.validation.split not in {"dev", "test"}, "validation.split", "independent validation cannot use the dev or test split")

    if config.benchmark.execution_split == "test":
        require(mode == "disabled", "benchmark.execution_split", "final test tasks cannot trigger evolution; use evolution.mode=disabled")
        require(config.agent.initial_snapshot_ref is not None, "agent.initial_snapshot_ref", "final test evaluation requires a frozen Snapshot manifest")

    if mode == "disabled":
        require(rounds == 0, "limits.max_evolution_rounds", "disabled mode requires 0 evolution rounds")
        require(routing.plugin is None, "routing.plugin", "disabled mode requires null")
        require(routing.fixed_target is None, "routing.fixed_target", "disabled mode requires null")
        require(not routing.manual_decisions, "routing.manual_decisions", "disabled mode requires an empty list")
        require(not targets.allowed, "targets.allowed", "disabled mode requires an empty list")
        require(not targets.default_operators, "targets.default_operators", "disabled mode requires an empty mapping")
    else:
        require(router is not None and mode in router.routing_modes, "routing.plugin", f"selected Router must declare support for {mode} mode")
        require(bool(targets.allowed), "targets.allowed", "enabled evolution requires at least one target")
        require(set(targets.default_operators) == set(targets.allowed), "targets.default_operators", "must provide exactly one operator for each allowed target")
        if mode == "fixed":
            require(routing.fixed_target in targets.allowed, "routing.fixed_target", "must name an allowed target")
            require(not routing.manual_decisions, "routing.manual_decisions", "fixed mode requires an empty list")
        elif mode == "manual":
            require(routing.fixed_target is None, "routing.fixed_target", "manual mode requires null")
            plan = routing.manual_decisions
            require(len(plan) <= rounds, "routing.manual_decisions", "decision count must not exceed max_evolution_rounds")
            require(bool(plan) == (rounds > 0), "routing.manual_decisions", "provide a non-empty plan for positive rounds, or an empty plan for 0 rounds")
            require([item.round_id for item in plan] == list(range(1, len(plan) + 1)), "routing.manual_decisions", "round_id values must be unique, consecutive and ordered from 1")
            for index, item in enumerate(plan):
                require(item.target in targets.allowed, f"routing.manual_decisions[{index}].target", "must name an allowed target")
        else:
            require(routing.fixed_target is None, "routing.fixed_target", "llm mode requires null")
            require(not routing.manual_decisions, "routing.manual_decisions", "llm mode requires an empty list")

    require(set(targets.operator_configs) <= set(targets.allowed), "targets.operator_configs", "configuration keys must name allowed targets")

    for index, target in enumerate(targets.allowed):
        if agent:
            require(target in agent.targets, f"targets.allowed[{index}]", "target is not supported by the Agent")
    for target, operator_id in targets.default_operators.items():
        field = f"targets.default_operators.{target}"
        operator = reference("evolution", operator_id, field)
        if operator:
            require(target in operator.targets, field, "Operator does not support this target")
            require(config.agent.plugin in operator.compatible_agents, field, "Operator is not compatible with the Agent")
            if operator.config_model:
                settings = {**targets.operator_configs.get(target, {}), "max_candidates": config.evolution.max_candidates_per_round}
                try:
                    operator.config_model.model_validate(settings)
                except ValidationError as error:
                    issues.extend(schema_issues(error, f"targets.operator_configs.{target}"))
        require("max_candidates" not in targets.operator_configs.get(target, {}), f"targets.operator_configs.{target}", "set candidate count with evolution.max_candidates_per_round")
    return issues
