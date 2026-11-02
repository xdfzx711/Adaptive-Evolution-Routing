from pydantic import model_validator

from aer_core.config.models import NonBlank, PositiveFloat, PositiveInt, Settings


class SkillClawConfig(Settings):
    max_candidates: PositiveInt = 1
    source_path: NonBlank | None = None
    max_skill_changes: PositiveInt = 4
    max_model_calls: PositiveInt = 8
    max_tokens: PositiveInt = 24000
    max_output_tokens: PositiveInt = 4096
    timeout_seconds: PositiveFloat = 180.0
    model_timeout_seconds: PositiveFloat = 60.0
    max_events: PositiveInt = 200
    max_field_chars: PositiveInt = 8000
    max_feedback_chars: PositiveInt = 16000
    verify_candidates: bool = False

    @model_validator(mode="after")
    def one_library(self):
        if self.max_candidates != 1:
            raise ValueError("SkillClaw exports one candidate skill library per round")
        return self
