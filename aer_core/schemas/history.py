"""Public component edits and numeric outcomes for evolution history."""

from math import isclose
from typing import Literal

from pydantic import model_validator

from .base import DomainModel, Identifier, Reward, Score
from .states import ValidationStatusValue


class ComponentEdit(DomainModel):
    component: Identifier
    operation: Literal["insert", "delete", "replace"]
    before: str
    after: str
    truncated: bool = False


class ScoreChange(DomainModel):
    before: Score
    after: Score
    delta: Reward

    @model_validator(mode="after")
    def observed_difference(self):
        if not isclose(self.delta, self.after - self.before, abs_tol=1e-12):
            raise ValueError("history score delta must match the observed scores")
        return self


class ValidationSummary(DomainModel):
    """Only disclosed scores; no task examples, answers or evidence references."""

    status: ValidationStatusValue
    current_task: ScoreChange | None = None
    independent: ScoreChange | None = None
    similar: ScoreChange | None = None
    regression: ScoreChange | None = None
