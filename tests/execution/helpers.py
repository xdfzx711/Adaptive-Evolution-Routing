from dataclasses import replace

from aer_core.config.models import AgentSettings, ModelProviderSettings, ToolProviderSettings
from aer_core.schemas.providers import ModelResponse
from aer_core.schemas.references import AgentSnapshot
from aer_core.schemas.states import RunStage
from aer_core.schemas.usage import UsageReport
from plugins.agents.react import ReActAgent, SingleCallAgent
from plugins.mocks import MockArtifactStore
from plugins.providers.scripted import ScriptedModel
from plugins.providers.tools import DeterministicTools
from tests.domain_helpers import run_context


MODEL = ModelProviderSettings(plugin="scripted_model", model_id="scripted-v0.1", config={"scenario_id": "mvp_v0_1_contract"})
TOOLS = ToolProviderSettings(plugin="deterministic_tools", enabled=["calculator"])


def context(**kwargs):
    values = {"operation_id": "exec-test", "stage": RunStage.INITIAL_EXECUTION, "budget": None}
    values.update(kwargs)
    return replace(run_context(), **values)


def agent_fixture(single=False, model=None, tools=None, max_steps=None):
    name = "single_call_agent" if single else "react_agent"
    steps = max_steps if max_steps is not None else 1 if single else 8
    store = MockArtifactStore()
    prompt = store.put(b"Complete the task and return its declared JSON output.", "prompt")
    snapshot = AgentSnapshot(version_id="initial-test", agent_plugin_id=name,
        config={"max_steps": steps, "model": MODEL.model_dump(mode="json"), "tools": TOOLS.model_dump(mode="json")},
        artifacts={"prompt": prompt})
    settings = AgentSettings(plugin=name, initial_prompt="fixture", initial_snapshot_ref=None, max_steps=steps)
    cls = SingleCallAgent if single else ReActAgent
    agent = cls(settings, snapshot, store, model or ScriptedModel(MODEL),
                None if single else tools or DeterministicTools(TOOLS))
    return agent, snapshot, store


def response(content=None, tool_calls=(), unknown=False):
    return ModelResponse(content=content, tool_calls=tool_calls, finish_reason="tool_calls" if tool_calls else "stop",
        usage=UsageReport(input_tokens=None if unknown else 3, output_tokens=None if unknown else 2,
                          model_calls=1, tool_calls=0, elapsed_seconds=0.001,
                          usage_source="unknown" if unknown else "simulated"))


class RecordingModel:
    def __init__(self, make_response):
        self.make_response = make_response
        self.requests = []

    def generate(self, request, run_ctx):
        self.requests.append((request, run_ctx))
        return self.make_response(request, run_ctx)
