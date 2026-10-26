"""Serializable acceptance objectives, distinct from raw evaluation scores."""

from math import isclose

from pydantic import model_validator

from .base import DomainModel, Reward, Score


class AcceptanceWeights(DomainModel):
    current_task: Score = 0.5
    similar: Score = 0.25
    regression: Score = 0.25

    @model_validator(mode="after")
    def normalized(self):
        if not isclose(self.current_task + self.similar + self.regression, 1, rel_tol=0, abs_tol=1e-12):
            raise ValueError("acceptance weights must sum to 1")
        return self


class WeightedGain(DomainModel):
    weights: AcceptanceWeights
    current_task: Reward
    similar: Reward
    regression: Reward
    total: Reward

    @model_validator(mode="after")
    def contribution_sum(self):
        if not isclose(self.total, self.current_task + self.similar + self.regression, abs_tol=1e-12):
            raise ValueError("weighted gain must equal the sum of contributions")
        return self


def validate_reward(reward, independent, similar, regression, current_task, weighted_gain):
    """Keep legacy records readable while checking the new reward breakdown."""
    if reward is None:
        if any(value is not None for value in (independent, similar, regression, current_task, weighted_gain)):
            raise ValueError("unknown reward requires null deltas and weighted gain")
        return
    if any(value is None for value in (independent, similar, regression)):
        raise ValueError("known reward requires all independent validation deltas")
    if weighted_gain is None:
        if not isclose(reward, independent, abs_tol=1e-12):
            raise ValueError("legacy reward must match delta_score")
        return
    if current_task is None:
        raise ValueError("weighted reward requires the current task delta")
    for name, delta in (("current_task", current_task), ("similar", similar), ("regression", regression)):
        if not isclose(getattr(weighted_gain, name), getattr(weighted_gain.weights, name) * delta, abs_tol=1e-12):
            raise ValueError("weighted contributions must match weights and observed deltas")
    if not isclose(reward, weighted_gain.total, abs_tol=1e-12):
        raise ValueError("weighted reward must match weighted gain")
