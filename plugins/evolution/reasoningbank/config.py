from typing import Literal

from aer_core.config.models import NonBlank, PositiveInt, Settings


class ReasoningBankConfig(Settings):
    max_candidates: Literal[1] = 1
    source_path: NonBlank | None = None
    max_output_tokens: PositiveInt = 4096
