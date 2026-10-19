from typing import Protocol, runtime_checkable

from aer_core.schemas.evolution import EvolutionContext, EvolutionDecision


@runtime_checkable
class RouterPlugin(Protocol):
    """Choose only from feasible_targets; never resolve an Operator or commit.

    Model routers use context.run_context for execution settings, report actual
    costs in decision.usage and attach an immutable call receipt in record_ref.
    Failed calls retain costs/evidence in RoutingError. Runtime settings are not
    model feedback and must be excluded from model input.

    Preserve select(context). Raise RoutingError for an invalid decision and
    UnsupportedCapability for an unavailable configured target.
    """

    def select(self, context: EvolutionContext) -> EvolutionDecision: ...
