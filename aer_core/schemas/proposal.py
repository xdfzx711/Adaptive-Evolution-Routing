"""A standalone generation attempt; acceptance and commit are later stages."""

from typing import Literal

from pydantic import model_validator

from .base import DomainModel, Identifier, Items, PositiveInt
from .evolution import EvolutionContext, EvolutionDecision, EvolutionResponse
from .experience import EvolutionAttempt
from .references import RecordRef
from .states import StopReasonValue


class EvolutionProposalRecord(DomainModel):
    run_id: Identifier
    episode_id: Identifier
    task_id: Identifier
    stable_version_id: Identifier
    round_id: PositiveInt
    status: Literal["skipped", "finished"]
    context: EvolutionContext | None
    attempt: EvolutionAttempt | None
    decision: EvolutionDecision | None
    operator_id: Identifier | None
    response: EvolutionResponse | None
    stop_reason: StopReasonValue | None
    experience_refs: Items[RecordRef]

    @model_validator(mode="after")
    def association(self):
        if self.status == "skipped":
            if self.stop_reason is None or self.attempt is not None or self.response is not None:
                raise ValueError("skipped proposal requires a stop reason and no attempt/response")
        elif self.attempt is None or self.response is None or self.stop_reason is not None:
            raise ValueError("finished proposal requires an attempt/response and no preflight stop reason")
        if self.context is not None and (self.context.round_id != self.round_id or self.context.task.task_id != self.task_id or self.context.stable_snapshot.version_id != self.stable_version_id):
            raise ValueError("context must match proposal task, round and stable version")
        if self.attempt is not None and (self.attempt.run_id, self.attempt.episode_id, self.attempt.task_id,
            self.attempt.round_id, self.attempt.stable_version_id) != (self.run_id, self.episode_id, self.task_id, self.round_id, self.stable_version_id):
            raise ValueError("attempt must match proposal scope")
        return self
