"""Public construction evidence, independent of candidate creation and task scoring."""

from typing import Literal
from pydantic import Field, model_validator

from .base import DomainModel, Identifier, Items, JsonObject, NonNegativeInt
from .references import ArtifactRef


class ConstructionIssue(DomainModel):
    stage: Literal["planning", "construction", "judgment", "delivery", "request", "termination"]
    code: Identifier
    tool: Identifier | None = None
    source: Literal["observation", "model_assessment"]
    status: Literal["unresolved", "resolved"] = "unresolved"
    message: str
    event_index: NonNegativeInt | None = None
    call_index: NonNegativeInt | None = None
    repair_index: NonNegativeInt | None = None
    resolution_event_index: NonNegativeInt | None = None
    resolution_call_index: NonNegativeInt | None = None
    details: JsonObject = Field(default_factory=dict)
    resolution_details: JsonObject = Field(default_factory=dict)
    truncated: bool = False


class ConstructionFeedback(DomainModel):
    version: Literal["aer.construction_feedback.v1"] = "aer.construction_feedback.v1"
    component: Literal["tool"] = "tool"
    operator_id: Identifier
    outcome: Literal["completed", "empty", "error", "budget_interrupted"]
    issues: Items[ConstructionIssue] = ()
    exported_tools: Items[Identifier] = ()
    record_ref: ArtifactRef | None = None
    omitted_issues: NonNegativeInt = 0
    truncated: bool = False

    @model_validator(mode="after")
    def public_receipt(self):
        if self.record_ref is not None and self.record_ref.kind != "tool_construction":
            raise ValueError("Construction feedback may reference only a tool_construction receipt")
        return self
