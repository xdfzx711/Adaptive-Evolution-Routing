"""Immutable construction receipts, separate from outer validation outcomes."""

from typing import Literal
from pydantic import Field

from aer_core.schemas.base import DomainModel, Identifier, Items, JsonObject, UtcTimestamp
from aer_core.schemas.execution import ErrorInfo
from aer_core.schemas.usage import ResourceUsage


class ToolConstructionRecord(DomainModel):
    schema_version: Literal["aer.tool_construction.v1"] = "aer.tool_construction.v1"
    operator: Identifier = "oscopilot_tool_evolver"
    attempt_id: Identifier
    task_id: Identifier
    parent_version_id: Identifier
    upstream_revision: Identifier
    adapter_version: Identifier = "2.7.0"
    retrieval: Literal["lexical_description"] = "lexical_description"
    started_at: UtcTimestamp
    finished_at: UtcTimestamp
    config: JsonObject
    status: Literal["completed", "empty", "error", "budget_interrupted"]
    calls: Items[JsonObject]
    events: Items[JsonObject]
    timings: JsonObject = Field(default_factory=dict)
    usage: ResourceUsage
    error: ErrorInfo | None = None
