"""Versioned execution-model selection; unrelated to the framework's provider."""

from typing import Literal

from aer_core.schemas.base import DomainModel


SUPPORTED_MODELS = ("gpt-6.1-sol", "gpt-6-sol", "gpt-6-luna", "gpt-5.6-sol")
ModelId = Literal["gpt-6.1-sol", "gpt-6-sol", "gpt-6-luna", "gpt-5.6-sol"]


class ModelSelection(DomainModel):
    model_id: ModelId
