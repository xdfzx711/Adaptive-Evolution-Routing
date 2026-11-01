"""Private native products, distinct from both AER candidates and active snapshots."""

from dataclasses import dataclass


@dataclass(frozen=True)
class ConstructedTool:
    name: str
    source: str
    invoke: str
    description: str
    response: str
    interface: dict | None
    result: dict


@dataclass(frozen=True)
class ConstructionResult:
    tools: tuple[ConstructedTool, ...]
    rationale: str
