"""Serializable resource reports and reservations; no budget implementation."""

from typing import Literal

from pydantic import model_validator

from .base import (
    DomainModel, Identifier, Items, NonNegativeFloat, NonNegativeInt,
)
from .states import StageValue


UsageSource = Literal["provider_reported", "simulated", "unknown"]


class UsageReport(DomainModel):
    input_tokens: NonNegativeInt | None
    output_tokens: NonNegativeInt | None
    model_calls: NonNegativeInt
    tool_calls: NonNegativeInt
    elapsed_seconds: NonNegativeFloat
    usage_source: UsageSource


class Reservation(DomainModel):
    operation_id: Identifier
    stage: StageValue
    reserved_tokens: NonNegativeInt
    reserved_tool_calls: NonNegativeInt
    status: Literal["pending", "settled"]
    reported_usage: UsageReport | None = None

    @model_validator(mode="after")
    def settlement_has_usage(self):
        if self.status == "settled" and (
            self.reported_usage is None
            or self.reported_usage.input_tokens is None
            or self.reported_usage.output_tokens is None
            or self.reported_usage.usage_source == "unknown"
        ):
            raise ValueError("settled reservations require known actual usage")
        return self


class BudgetRemaining(DomainModel):
    tokens: NonNegativeInt
    tool_calls: NonNegativeInt
    elapsed_seconds: NonNegativeFloat


class ResourceUsage(DomainModel):
    input_tokens: NonNegativeInt | None
    output_tokens: NonNegativeInt | None
    total_tokens: NonNegativeInt | None
    # External runtimes can report exact tokens without exposing request counts.
    model_calls: NonNegativeInt | None
    tool_calls: NonNegativeInt
    elapsed_seconds: NonNegativeFloat
    usage_complete: bool
    usage_sources: Items[UsageSource]
    pending_reservations: Items[Reservation] = ()

    @model_validator(mode="after")
    def accounting_is_consistent(self):
        known = self.input_tokens is not None and self.output_tokens is not None
        if known and self.total_tokens != self.input_tokens + self.output_tokens:
            raise ValueError("total_tokens must equal input_tokens + output_tokens")
        if not known and self.total_tokens is not None:
            raise ValueError("total_tokens must be null when token usage is unknown")
        if self.usage_complete and (
            not known or self.pending_reservations or "unknown" in self.usage_sources
        ):
            raise ValueError("complete usage requires known tokens and no pending reservations")
        if len(set(self.usage_sources)) != len(self.usage_sources):
            raise ValueError("usage_sources must be unique")
        ids = [item.operation_id for item in self.pending_reservations]
        if len(ids) != len(set(ids)) or any(item.status != "pending" for item in self.pending_reservations):
            raise ValueError("pending_reservations must have unique operation IDs and pending status")
        if self.model_calls and not self.usage_sources:
            raise ValueError("model calls require a usage source")
        return self

    @classmethod
    def zero(cls):
        return cls(
            input_tokens=0, output_tokens=0, total_tokens=0,
            model_calls=0, tool_calls=0, elapsed_seconds=0,
            usage_complete=True, usage_sources=(),
        )
