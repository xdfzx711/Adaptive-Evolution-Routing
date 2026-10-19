"""Six deterministic plugins for registration and replacement acceptance tests."""

from hashlib import sha256

from aer_core.config.models import AgentSettings, PositiveInt, RoutingSettings, Settings
from aer_core.contracts.storage import ArtifactStore
from aer_core.errors import ContractError, RoutingError, StorageError, UnsupportedCapability
from aer_core.plugin_registry import API_VERSION, PluginRegistry, ResolvedDependencies
from aer_core.schemas.evolution import AgentCapabilities, EvolutionCandidate, EvolutionCapabilities, EvolutionContext, EvolutionDecision, EvolutionRequest, EvolutionResponse
from aer_core.schemas.execution import ExecutionResult, TraceEvent
from aer_core.schemas.plugins import PluginCapabilities, PluginMetadata
from aer_core.schemas.references import AgentSnapshot, ArtifactRef
from aer_core.schemas.runtime import RunContext
from aer_core.schemas.task import TaskView
from aer_core.schemas.usage import ResourceUsage


MOCK_AGENTS = ("mock_agent_a", "mock_agent_b")


class MockArtifactStore:
    """Process-local bytes for fixture assembly; never creates local run files."""

    def __init__(self):
        self._content: dict[str, bytes] = {}

    def put(self, content: bytes, kind: str) -> ArtifactRef:
        digest = sha256(content).hexdigest()
        artifact_id = f"{kind}-{digest}"
        self._content[artifact_id] = content
        return ArtifactRef(artifact_id=artifact_id, kind=kind, version=digest, uri=f"memory://{artifact_id}", digest=digest)

    def get(self, ref: ArtifactRef) -> bytes:
        try:
            content = self._content[ref.artifact_id]
        except KeyError as error:
            raise StorageError(f"mock artifact not found: {ref.artifact_id}", stage="evolution") from error
        if sha256(content).hexdigest() != ref.digest:
            raise StorageError("mock artifact content identity differs from reference", stage="evolution")
        return content


class MockAgentA:
    plugin_id = "mock_agent_a"
    answer = "mock-a"

    def __init__(self, config: AgentSettings, initial_snapshot: AgentSnapshot):
        if config.plugin != self.plugin_id or initial_snapshot.agent_plugin_id != self.plugin_id:
            raise ContractError("Agent configuration and Snapshot must match the factory", stage="setup", plugin_id=self.plugin_id)
        self.config = config
        self._snapshot = initial_snapshot

    def capabilities(self) -> AgentCapabilities:
        return AgentCapabilities(supported_targets=["prompt"], supported_artifact_kinds=["prompt"], can_rebuild_session=True, snapshot_scope="controlled_config")

    def run(self, task: TaskView, snapshot: AgentSnapshot, run_ctx: RunContext) -> ExecutionResult:
        if snapshot.agent_plugin_id != self.plugin_id:
            raise UnsupportedCapability("Snapshot belongs to a different Agent", stage=run_ctx.stage, operation_id=run_ctx.operation_id, plugin_id=self.plugin_id)
        output = {"answer": self.answer, "agent": self.plugin_id}
        return ExecutionResult(task_id=task.task_id, agent_version_id=snapshot.version_id, operation_id=run_ctx.operation_id, output=output, trajectory=[TraceEvent(seq=0, kind="finish", payload=output)], state_summary={"mock": True}, usage=ResourceUsage.zero(), status="completed")

    def snapshot(self) -> AgentSnapshot:
        return self._snapshot

    def restore(self, snapshot: AgentSnapshot) -> None:
        if snapshot.agent_plugin_id != self.plugin_id:
            raise UnsupportedCapability("Snapshot belongs to a different Agent", stage="setup", plugin_id=self.plugin_id)
        self._snapshot = snapshot


class MockAgentB(MockAgentA):
    plugin_id = "mock_agent_b"
    answer = "mock-b"


class MockRouterA:
    def __init__(self, config: RoutingSettings):
        self.config = config

    def select(self, context: EvolutionContext) -> EvolutionDecision:
        target = self.config.fixed_target
        if target not in context.feasible_targets:
            raise RoutingError("fixed mock target is not feasible", stage="routing")
        return EvolutionDecision(target=target, rationale="mock router A: configured fixed target")


class MockRouterB(MockRouterA):
    def select(self, context: EvolutionContext) -> EvolutionDecision:
        if self.config.fixed_target is not None:
            decision = super().select(context)
            return decision.replace(rationale="mock router B: configured fixed target")
        round_id = context.round_id
        decision = next((item for item in self.config.manual_decisions if item.round_id == round_id), None)
        if decision is None:
            raise UnsupportedCapability("manual mock plan is exhausted", stage="routing")
        if decision.target not in context.feasible_targets:
            raise RoutingError("manual mock target is not feasible", stage="routing")
        return EvolutionDecision(target=decision.target, rationale=decision.rationale)


class MockEvolverConfig(Settings):
    max_candidates: PositiveInt


class MockPromptEvolverA:
    plugin_id = "mock_prompt_evolver_a"
    suffix = "\nMock amendment A."

    def __init__(self, config: MockEvolverConfig, artifact_store: ArtifactStore):
        self.config = config
        self.artifact_store = artifact_store

    def capabilities(self) -> EvolutionCapabilities:
        return EvolutionCapabilities(supported_targets=["prompt"], compatible_agents=MOCK_AGENTS, required_artifact_kinds=["prompt"], supports_multiple_candidates=True, reports_usage=True)

    def evolve(self, request: EvolutionRequest) -> EvolutionResponse:
        stable = request.context.stable_snapshot
        old = self.artifact_store.get(stable.artifacts["prompt"])
        candidates = []
        for index in range(min(self.config.max_candidates, request.max_candidates)):
            content = old + f"{self.suffix} Candidate {index + 1}.".encode("utf-8")
            candidates.append(EvolutionCandidate(candidate_id=f"{self.plugin_id}-{request.attempt_id}-{index + 1}", parent_version_id=stable.version_id, target=request.decision.target, artifact_updates={"prompt": self.artifact_store.put(content, "prompt")}, metadata={"mock": True}))
        return EvolutionResponse(candidates=candidates, usage=ResourceUsage.zero(), status="completed")


class MockPromptEvolverB(MockPromptEvolverA):
    plugin_id = "mock_prompt_evolver_b"
    suffix = "\nMock amendment B."


def build_agent_a(*, config: AgentSettings, dependencies: ResolvedDependencies) -> MockAgentA:
    return MockAgentA(config, dependencies.get("initial_snapshot"))


def build_agent_b(*, config: AgentSettings, dependencies: ResolvedDependencies) -> MockAgentB:
    return MockAgentB(config, dependencies.get("initial_snapshot"))


def build_router_a(*, config: RoutingSettings, dependencies: ResolvedDependencies) -> MockRouterA:
    return MockRouterA(config)


def build_router_b(*, config: RoutingSettings, dependencies: ResolvedDependencies) -> MockRouterB:
    return MockRouterB(config)


def build_evolver_a(*, config: MockEvolverConfig, dependencies: ResolvedDependencies) -> MockPromptEvolverA:
    return MockPromptEvolverA(config, dependencies.get("artifact_store"))


def build_evolver_b(*, config: MockEvolverConfig, dependencies: ResolvedDependencies) -> MockPromptEvolverB:
    return MockPromptEvolverB(config, dependencies.get("artifact_store"))


def register_mock_plugins(registry: PluginRegistry) -> None:
    for plugin_id, factory in [("mock_agent_a", build_agent_a), ("mock_agent_b", build_agent_b)]:
        registry.register(PluginMetadata(id=plugin_id, category="agent", api_version=API_VERSION, plugin_version="0.1.0", capabilities=PluginCapabilities(targets=["prompt"]), dependencies=["initial_snapshot"]), factory, config_model=AgentSettings, dependency_types={"initial_snapshot": AgentSnapshot})
    for plugin_id, factory, modes in [("mock_router_a", build_router_a, ["fixed"]), ("mock_router_b", build_router_b, ["fixed", "manual"])]:
        registry.register(PluginMetadata(id=plugin_id, category="router", api_version=API_VERSION, plugin_version="0.1.0", capabilities=PluginCapabilities(routing_modes=modes)), factory, config_model=RoutingSettings)
    for plugin_id, factory in [("mock_prompt_evolver_a", build_evolver_a), ("mock_prompt_evolver_b", build_evolver_b)]:
        registry.register(PluginMetadata(id=plugin_id, category="evolution", api_version=API_VERSION, plugin_version="0.1.0", capabilities=PluginCapabilities(targets=["prompt"], compatible_agents=MOCK_AGENTS), dependencies=["artifact_store"]), factory, config_model=MockEvolverConfig, dependency_types={"artifact_store": ArtifactStore})
