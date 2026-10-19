"""Only interface instances are passed across the controller boundary."""

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

from .agent import AgentPlugin
from .evolution import EvolutionPlugin
from .router import RouterPlugin


@dataclass(frozen=True)
class ControllerPlugins:
    agent: AgentPlugin
    router: RouterPlugin | None
    operators: Mapping[str, EvolutionPlugin]

    def __post_init__(self):
        object.__setattr__(self, "operators", MappingProxyType(dict(self.operators)))
