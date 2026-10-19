from typing import Protocol, runtime_checkable

from aer_core.schemas.evolution import EvolutionCapabilities, EvolutionRequest, EvolutionResponse


@runtime_checkable
class EvolutionPlugin(Protocol):
    """Generate isolated candidates; never modify or promote the stable version.

    Respect request.max_candidates, report all model/search usage and raise
    EvolutionError with known usage when no normal response is available.
    """

    def capabilities(self) -> EvolutionCapabilities: ...

    def evolve(self, request: EvolutionRequest) -> EvolutionResponse: ...
