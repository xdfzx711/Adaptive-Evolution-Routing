"""Errors that can be presented without a Python traceback."""

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from aer_core.schemas.experience import VersionCommitRecord
    from aer_core.schemas.execution import ErrorInfo
    from aer_core.schemas.references import ArtifactRef
    from aer_core.schemas.states import RunStage
    from aer_core.schemas.usage import ResourceUsage


@dataclass(frozen=True)
class ConfigIssue:
    field: str
    message: str


class ConfigError(ValueError):
    """One or more errors in a configuration or its referenced inputs."""

    def __init__(self, source: Path, issues: list[ConfigIssue]):
        self.source = source
        self.issues = tuple(issues)
        details = "\n".join(f"  {issue.field}: {issue.message}" for issue in issues)
        super().__init__(f"Invalid configuration: {source}\n{details}")


class AERError(RuntimeError):
    """Structured runtime failure, retaining known usage even on failure."""

    code = "aer_error"

    def __init__(
        self, message: str, *, stage: "RunStage | str",
        operation_id: str | None = None, plugin_id: str | None = None,
        evidence_ref: "ArtifactRef | None" = None,
        usage: "ResourceUsage | None" = None,
    ):
        from aer_core.schemas.execution import ErrorInfo

        self.info = ErrorInfo(
            code=self.code, stage=stage, message=message,
            operation_id=operation_id, plugin_id=plugin_id,
            evidence_ref=evidence_ref, usage=usage,
        )
        super().__init__(message)

    def to_info(self) -> "ErrorInfo":
        return self.info


class PluginError(AERError):
    code = "plugin_error"


class UnsupportedCapability(PluginError):
    code = "unsupported_capability"


class ContractError(PluginError):
    code = "contract_error"


class ExecutionError(PluginError):
    code = "execution_error"


class EvaluationError(PluginError):
    code = "evaluation_error"


class BenchmarkError(PluginError):
    code = "benchmark_error"


class DiagnosisError(PluginError):
    code = "diagnosis_error"


class RoutingError(PluginError):
    code = "routing_error"


class ManualPlanExhausted(UnsupportedCapability):
    code = "manual_plan_exhausted"


class NoFeasibleTargets(UnsupportedCapability):
    code = "no_feasible_targets"


class EvolutionError(PluginError):
    code = "evolution_error"


class ValidationError(PluginError):
    code = "validation_error"


class CommitError(PluginError):
    code = "commit_error"


class CommitFinalizationError(CommitError):
    """The pointer was published; retry/recovery must finish its durable log."""

    code = "commit_finalization_error"

    def __init__(self, message: str, *, committed_record: "VersionCommitRecord"):
        self.committed_record = committed_record
        super().__init__(message, stage="commit", operation_id=committed_record.attempt_id)


class StorageError(PluginError):
    code = "storage_error"


class OperationTimeout(PluginError):
    code = "operation_timeout"


class BudgetExceeded(AERError):
    code = "budget_exceeded"


class PluginRegistrationError(PluginError):
    code = "plugin_registration_error"


class DuplicatePluginError(PluginRegistrationError):
    code = "duplicate_plugin"


class PluginNotFound(PluginError):
    code = "plugin_not_found"


class PluginVersionError(PluginRegistrationError):
    code = "plugin_version_error"


class PluginConfigurationError(PluginError):
    code = "plugin_configuration_error"


class DependencyError(PluginError):
    code = "dependency_error"


class PluginInstantiationError(PluginError):
    code = "plugin_instantiation_error"
