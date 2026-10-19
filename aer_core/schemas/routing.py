"""Exact model routing input, response and actual call usage."""

from .base import DomainModel, Identifier, UtcTimestamp
from .evolution import EvolutionDecision
from .execution import ErrorInfo
from .providers import ModelRequest, ModelResponse
from .usage import ResourceUsage


class RoutingRecord(DomainModel):
    run_id: Identifier
    episode_id: Identifier
    operation_id: Identifier
    started_at: UtcTimestamp
    finished_at: UtcTimestamp
    request: ModelRequest
    response: ModelResponse | None
    decision: EvolutionDecision | None
    usage: ResourceUsage
    error: ErrorInfo | None
