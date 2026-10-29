"""Search policy belongs to the TroVE plugin, not the Evolution Router."""

from typing import Literal

from pydantic import Field

from aer_core.config.models import PositiveFloat, PositiveInt, Settings


class TroveConfig(Settings):
    max_candidates: PositiveInt
    samples_per_mode: int = Field(default=2, ge=1, le=8)
    selection_strategy: Literal["tool_checks", "execution_agreement"] = "tool_checks"
    max_library_tools: int = Field(default=32, ge=1, le=128)
    check_timeout_seconds: PositiveFloat = 5.0
    max_search_seconds: PositiveFloat = 120.0
