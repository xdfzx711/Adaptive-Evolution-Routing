"""Declarative metadata for checking references, not a plugin factory registry."""

from dataclasses import dataclass

from pydantic import BaseModel


@dataclass(frozen=True)
class PluginDeclaration:
    category: str
    plugin_id: str
    targets: tuple[str, ...] = ()
    compatible_agents: tuple[str, ...] = ()
    tools: tuple[str, ...] = ()
    splits: tuple[str, ...] = ()
    benchmark_id: str | None = None
    policy_versions: tuple[str, ...] = ()
    config_model: type[BaseModel] | None = None
    routing_modes: tuple[str, ...] = ()
    fixed_max_steps: int | None = None


@dataclass(frozen=True)
class ConfigCatalog:
    declarations: tuple[PluginDeclaration, ...]

    def __post_init__(self) -> None:
        keys = [(item.category, item.plugin_id) for item in self.declarations]
        if len(keys) != len(set(keys)):
            raise ValueError("duplicate category/plugin_id in configuration catalog")

    def get(self, category: str, plugin_id: str) -> PluginDeclaration | None:
        return next(
            (item for item in self.declarations
             if item.category == category and item.plugin_id == plugin_id),
            None,
        )

    def names(self, category: str) -> tuple[str, ...]:
        return tuple(sorted(item.plugin_id for item in self.declarations if item.category == category))
