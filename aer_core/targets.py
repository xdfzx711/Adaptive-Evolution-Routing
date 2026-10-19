"""Resolve configured operators and report targets executable by this Agent."""

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

from aer_core.contracts.agent import AgentPlugin
from aer_core.contracts.evolution import EvolutionPlugin
from aer_core.errors import RoutingError
from aer_core.schemas.base import DomainModel, Identifier, Items
from aer_core.schemas.references import AgentSnapshot


class TargetAvailability(DomainModel):
    target: Identifier
    operator_id: Identifier | None
    feasible: bool
    reason_code: Identifier


class FeasibleTargets(DomainModel):
    targets: Items[Identifier]
    availability: Items[TargetAvailability]


@dataclass(frozen=True)
class OperatorBinding:
    plugin_id: str
    operator: EvolutionPlugin


class OperatorResolver:
    def __init__(self, defaults: Mapping[str, str], operators: Mapping[str, OperatorBinding]):
        self.defaults = MappingProxyType(dict(defaults))
        self.operators = MappingProxyType(dict(operators))

    def resolve(self, target: str) -> OperatorBinding:
        plugin_id = self.defaults.get(target)
        binding = self.operators.get(target)
        if binding is None or plugin_id is None or binding.plugin_id != plugin_id:
            raise RoutingError("Configured operator is not assembled for the target", stage="routing")
        if target not in binding.operator.capabilities().supported_targets:
            raise RoutingError("Configured operator does not support the target", stage="routing", plugin_id=plugin_id)
        return binding


class TargetRegistry:
    def __init__(self, resolver: OperatorResolver):
        self.resolver = resolver

    def feasible(self, agent: AgentPlugin, snapshot: AgentSnapshot, allowed) -> FeasibleTargets:
        capabilities = agent.capabilities()
        results = []
        for target in dict.fromkeys(allowed):
            operator_id = self.resolver.defaults.get(target)
            reason = "available"
            if target not in capabilities.supported_targets:
                reason = "agent_target_unsupported"
            elif not capabilities.can_rebuild_session:
                reason = "session_rebuild_unsupported"
            else:
                try:
                    operator = self.resolver.resolve(target).operator.capabilities()
                except RoutingError:
                    reason = "operator_unavailable"
                else:
                    if snapshot.agent_plugin_id not in operator.compatible_agents:
                        reason = "agent_operator_incompatible"
                    elif target not in snapshot.artifacts:
                        reason = "target_artifact_missing"
                    elif not set(operator.required_artifact_kinds).issubset(capabilities.supported_artifact_kinds):
                        reason = "agent_artifact_unsupported"
                    elif not set(operator.required_artifact_kinds).issubset({ref.kind for ref in snapshot.artifacts.values()}):
                        reason = "required_artifact_missing"
                    elif snapshot.artifacts[target].kind != target:
                        reason = "target_artifact_kind_mismatch"
            results.append(TargetAvailability(target=target, operator_id=operator_id,
                                              feasible=reason == "available", reason_code=reason))
        return FeasibleTargets(targets=[item.target for item in results if item.feasible], availability=results)
