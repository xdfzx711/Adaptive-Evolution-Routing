"""Deterministic step-limit proposals; no model calls and no stable writes."""

from time import monotonic

from aer_core.config.models import PositiveInt, Settings
from aer_core.errors import AERError, EvolutionError
from aer_core.schemas.evolution import EvolutionCandidate, EvolutionCapabilities, EvolutionResponse
from aer_core.schemas.usage import ResourceUsage
from plugins.agents.limits import ExecutionLimits


class StepLimitEvolverConfig(Settings):
    max_candidates: PositiveInt
    step_increment: PositiveInt
    max_steps: PositiveInt


class StepLimitEvolver:
    plugin_id = "step_limit_evolver"

    def __init__(self, config, artifacts):
        self.config, self.artifacts = config, artifacts

    def capabilities(self):
        return EvolutionCapabilities(supported_targets=["execution_limits"], compatible_agents=["react_agent"],
            required_artifact_kinds=["execution_limits"], supports_multiple_candidates=False, reports_usage=True,
            target_descriptions={"execution_limits": (
                f"Increase ReAct's maximum model-call steps by {self.config.step_increment}, capped at {self.config.max_steps}. "
                "Useful when execution reaches the step limit before finishing; does not change prompts or tool implementations. "
                "Returns no candidate if the current limit already reaches the cap.")})

    def evolve(self, request):
        start = monotonic()
        usage = ResourceUsage.zero()
        try:
            stable = request.context.stable_snapshot
            if request.decision.target != "execution_limits" or stable.agent_plugin_id not in self.capabilities().compatible_agents:
                raise ValueError("unsupported evolution target or Agent")
            old = ExecutionLimits.model_validate_json(self.artifacts.get(stable.artifacts["execution_limits"]))
            new_limit = min(old.max_steps + self.config.step_increment, self.config.max_steps)
            candidates = []
            if new_limit > old.max_steps:
                ref = self.artifacts.put(ExecutionLimits(max_steps=new_limit).model_dump_json().encode("utf-8"), "execution_limits")
                candidates.append(EvolutionCandidate(candidate_id=f"{request.attempt_id}/candidate/1",
                    parent_version_id=stable.version_id, target="execution_limits", artifact_updates={"execution_limits": ref},
                    metadata={"rationale": f"Increase max_steps from {old.max_steps} to {new_limit}.",
                              "operator": self.plugin_id, "round_id": request.round_id}))
            return EvolutionResponse(candidates=candidates, usage=usage.replace(elapsed_seconds=monotonic() - start),
                                     status="completed" if candidates else "empty")
        except Exception as failure:
            usage = usage.replace(elapsed_seconds=monotonic() - start)
            error = failure.to_info().replace(stage="evolution", usage=usage) if isinstance(failure, AERError) else EvolutionError(
                "Step-limit proposal failed: invalid target or execution_limits artifact", stage="evolution",
                plugin_id=self.plugin_id, operation_id=request.run_context.operation_id, usage=usage).to_info()
            return EvolutionResponse(candidates=[], usage=usage, status="error", error=error)
