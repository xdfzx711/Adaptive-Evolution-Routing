"""Exact model routing input, response and actual call usage."""

from typing import Literal

from .base import DomainModel, Identifier, JsonObject, UtcTimestamp
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


class JevRoutingRecord(DomainModel):
    """Exact typed decision request and response, without credentials."""

    run_id: Identifier
    episode_id: Identifier
    operation_id: Identifier
    source: Literal["jev", "single_feasible_target"] = "jev"
    started_at: UtcTimestamp
    finished_at: UtcTimestamp
    request: JsonObject
    response: JsonObject | None
    decision: EvolutionDecision | None
    usage: ResourceUsage
    error: ErrorInfo | None
