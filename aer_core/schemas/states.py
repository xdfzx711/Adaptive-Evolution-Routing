"""Explicit lifecycle states. State transitions belong to the Orchestrator."""

from enum import StrEnum
from typing import Annotated

from pydantic import BeforeValidator


class RunStage(StrEnum):
    SETUP = "setup"
    INITIAL_EXECUTION = "initial_execution"
    EVALUATION = "evaluation"
    DIAGNOSIS = "diagnosis"
    ROUTING = "routing"
    EVOLUTION = "evolution"
    LOCAL_VALIDATION = "local_validation"
    SIMILAR_VALIDATION = "similar_validation"
    REGRESSION_VALIDATION = "regression_validation"
    ACCEPTANCE = "acceptance"
    COMMIT = "commit"
    POST_COMMIT_EXECUTION = "post_commit_execution"
    FINISHED = "finished"


class RunStatus(StrEnum):
    CREATED = "created"
    RUNNING = "running"
    COMPLETED = "completed"
    ERROR = "error"
    BUDGET_INTERRUPTED = "budget_interrupted"


class ExecutionStatus(StrEnum):
    COMPLETED = "completed"
    STEP_LIMIT = "step_limit"
    BUDGET_INTERRUPTED = "budget_interrupted"
    ERROR = "error"


class EvolutionResponseStatus(StrEnum):
    COMPLETED = "completed"
    EMPTY = "empty"
    ERROR = "error"
    BUDGET_INTERRUPTED = "budget_interrupted"


class AttemptStatus(StrEnum):
    STARTED = "started"
    COMPLETED = "completed"
    EMPTY_CANDIDATES = "empty_candidates"
    ERROR = "error"
    BUDGET_INTERRUPTED = "budget_interrupted"


class ValidationStatus(StrEnum):
    COMPLETE = "complete"
    INCOMPLETE = "incomplete"
    ERROR = "error"


class CandidateStatus(StrEnum):
    GENERATED = "generated"
    VALIDATING = "validating"
    NOT_VALIDATED = "not_validated"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    INCOMPLETE = "incomplete"
    SELECTED = "selected"
    COMMITTED = "committed"
    COMMIT_FAILED = "commit_failed"


class StopReason(StrEnum):
    TASK_SUCCEEDED = "task_succeeded"
    EVOLUTION_DISABLED = "evolution_disabled"
    MAX_EVOLUTION_ROUNDS_REACHED = "max_evolution_rounds_reached"
    BUDGET_EXHAUSTED = "budget_exhausted"
    NO_FEASIBLE_TARGETS = "no_feasible_targets"
    MANUAL_PLAN_EXHAUSTED = "manual_plan_exhausted"
    NO_COMMIT_LIMIT_REACHED = "no_commit_limit_reached"
    EXECUTION_ERROR = "execution_error"
    EVALUATION_ERROR = "evaluation_error"
    DIAGNOSIS_ERROR = "diagnosis_error"
    STORAGE_ERROR = "storage_error"
    ROUTING_ERROR = "routing_error"
    EVOLUTION_ERROR = "evolution_error"
    VALIDATION_ERROR = "validation_error"
    COMMIT_ERROR = "commit_error"


def state_type(enum):
    # Enum values remain strict: only an existing enum or its exact string value
    # is accepted. The conversion also supports model_validate on JSON dicts.
    def convert(value):
        return enum(value) if isinstance(value, str) else value
    return Annotated[enum, BeforeValidator(convert)]


StageValue = state_type(RunStage)
RunStatusValue = state_type(RunStatus)
ExecutionStatusValue = state_type(ExecutionStatus)
ResponseStatusValue = state_type(EvolutionResponseStatus)
AttemptStatusValue = state_type(AttemptStatus)
ValidationStatusValue = state_type(ValidationStatus)
CandidateStatusValue = state_type(CandidateStatus)
StopReasonValue = state_type(StopReason)
