from aer_core.config.models import RoutingSettings
from aer_core.errors import ManualPlanExhausted, RoutingError
from aer_core.schemas.evolution import EvolutionContext, EvolutionDecision


class FixedRouter:
    def __init__(self, config: RoutingSettings):
        if config.fixed_target is None or config.manual_decisions:
            raise ValueError("Fixed Router requires a fixed target and no manual plan")
        self.target = config.fixed_target

    def select(self, context: EvolutionContext) -> EvolutionDecision:
        if self.target not in context.feasible_targets:
            raise RoutingError("Configured fixed target is not feasible", stage="routing", plugin_id="fixed_router")
        return EvolutionDecision(target=self.target, rationale="Select the configured fixed evolution target.")


class ManualRouter:
    def __init__(self, config: RoutingSettings):
        if config.fixed_target is not None:
            raise ValueError("Manual Router cannot have a fixed target")
        rounds = [item.round_id for item in config.manual_decisions]
        if rounds != list(range(1, len(rounds) + 1)):
            raise ValueError("Manual rounds must start at 1 and be continuous")
        self.plan = {item.round_id: item for item in config.manual_decisions}

    def select(self, context: EvolutionContext) -> EvolutionDecision:
        decision = self.plan.get(context.round_id)
        if decision is None:
            raise ManualPlanExhausted("No manual decision exists for this round", stage="routing", plugin_id="manual_router")
        if decision.target not in context.feasible_targets:
            raise RoutingError("Configured manual target is not feasible", stage="routing", plugin_id="manual_router")
        return EvolutionDecision(target=decision.target, rationale=decision.rationale)
