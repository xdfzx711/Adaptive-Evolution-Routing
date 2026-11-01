import json
import unittest

from aer_core.errors import OperationTimeout
from aer_core.schemas.evolution import EvolutionDecision, EvolutionRequest
from aer_core.schemas.usage import ResourceUsage
from plugins.agents.tool_selection import CodexToolAdapter
from plugins.evolution.tool_selection import ToolSelectionEvolver, ToolSelectionEvolverConfig
from plugins.tool_selection import ToolDescriptor, ToolSelection
from plugins.tools.catalog import ToolingConfig
from tests.evolution.helpers import evolution_context
from tests.execution.helpers import RecordingModel, response
from tests.tools.helpers import LOCAL_CATALOG


class ToolEvolverTests(unittest.TestCase):
    def test_selecting_generated_tools_preserves_their_implementations(self):
        from plugins.tools.generated.bundle import ToolBundle, decode_tools
        from tests.tools.test_generated import tool
        artifacts, request = self.request()
        before = ToolBundle(enabled_tools=[], generated_tools={"source_summary": tool()})
        stable = request.context.stable_snapshot.replace(config={
            **request.context.stable_snapshot.model_dump(mode="json")["config"], "generated_tools_enabled": True},
            artifacts={**request.context.stable_snapshot.artifacts, "tool": artifacts.put(before.model_dump_json().encode(), "tool")})
        request = EvolutionRequest(attempt_id=request.attempt_id, round_id=1,
            context=request.context.replace(stable_snapshot=stable), decision=request.decision,
            operator_id=request.operator_id, max_candidates=1, run_context=request.run_context)
        model = RecordingModel(lambda *_: response('{"enabled_tools":["source_summary"],"rationale":"Use the existing generated implementation"}'))
        result = ToolSelectionEvolver(ToolSelectionEvolverConfig(max_candidates=1), artifacts, model,
                                      adapter=CodexToolAdapter()).evolve(request)
        self.assertEqual(result.status, "completed", result.error)
        selected = decode_tools(artifacts.get(result.candidates[0].artifact_updates["tool"]))
        self.assertEqual(selected.generated_tools, before.generated_tools)
        self.assertEqual(selected.enabled_tools, ("source_summary",))

    def request(self, selected=(), catalog=None):
        state, artifacts, live = evolution_context()
        cfg = ToolingConfig(catalog=LOCAL_CATALOG if catalog is None else catalog)
        ref = artifacts.put(CodexToolAdapter().encode(ToolSelection(enabled_tools=selected)), "tool")
        stable = state.stable_snapshot.replace(agent_plugin_id="codex_cli_agent",
            config={"tool_catalog": cfg.public_catalog()}, artifacts={**state.stable_snapshot.artifacts, "tool": ref})
        return artifacts, EvolutionRequest(attempt_id="tool-attempt", round_id=1,
            context=state.replace(stable_snapshot=stable, feasible_targets=["tool", "prompt"]),
            decision=EvolutionDecision(target="tool", rationale="observed execution evidence"),
            operator_id="tool_selection_evolver", max_candidates=1, run_context=live)

    def test_add_remove_and_replace_only_modify_the_tool_artifact(self):
        for before, after in (([], ["python_structure"]), (["python_structure"], []),
                              (["python_structure"], ["python_syntax_check"])):
            with self.subTest(before=before, after=after):
                artifacts, request = self.request(before)
                stable = request.context.stable_snapshot
                proposer = RecordingModel(lambda *_: response(json.dumps({"enabled_tools": after, "rationale": "test the change"})))
                operator = ToolSelectionEvolver(ToolSelectionEvolverConfig(max_candidates=1), artifacts, proposer, adapter=CodexToolAdapter())
                self.assertTrue(operator.is_applicable(stable))
                result = operator.evolve(request)
                self.assertEqual(result.status, "completed", result.error)
                candidate = result.candidates[0]
                self.assertEqual(set(candidate.artifact_updates), {"tool"})
                self.assertEqual(ToolSelection.model_validate_json(artifacts.get(candidate.artifact_updates["tool"])), ToolSelection(enabled_tools=after))
                self.assertEqual(request.context.stable_snapshot, stable)
                payload = json.loads(proposer.requests[0][0].messages[-1].content)
                self.assertEqual(payload["current_selection"]["enabled_tools"], before)
                self.assertEqual(set(payload["tool_catalog"]), set(LOCAL_CATALOG))
                self.assertNotIn("servers", payload)
                self.assertEqual(result.usage.model_calls, 1)

    def test_empty_identical_unknown_duplicate_and_nonselection_responses_preserve_costs(self):
        artifacts, request = self.request(["python_structure", "python_syntax_check"])
        for chosen, expected in ((None, "empty"), (["python_syntax_check", "python_structure"], "empty"),
                                  (["missing"], "error"), (["python_structure", "python_structure"], "error")):
            with self.subTest(chosen=chosen):
                proposer = RecordingModel(lambda *_: response(json.dumps({"enabled_tools": chosen, "rationale": "test"})))
                result = ToolSelectionEvolver(ToolSelectionEvolverConfig(max_candidates=1), artifacts, proposer,
                    adapter=CodexToolAdapter()).evolve(request)
                self.assertEqual(result.status, expected)
                self.assertFalse(result.candidates)
                self.assertEqual(result.usage.model_calls, 1)

    def test_empty_catalog_is_inapplicable_and_provider_failure_retains_reported_usage(self):
        artifacts, request = self.request(catalog={})
        proposer = RecordingModel(lambda *_: self.fail("No call for an exhausted catalog"))
        operator = ToolSelectionEvolver(ToolSelectionEvolverConfig(max_candidates=1), artifacts, proposer, adapter=CodexToolAdapter())
        self.assertFalse(operator.is_applicable(request.context.stable_snapshot))
        self.assertEqual(operator.evolve(request).status, "empty")
        artifacts, request = self.request()
        usage = ResourceUsage.zero().replace(input_tokens=3, output_tokens=2, total_tokens=5,
            model_calls=1, usage_sources=["simulated"])
        def failed(*_):
            raise OperationTimeout("fixture", stage="evolution", usage=usage)
        result = ToolSelectionEvolver(ToolSelectionEvolverConfig(max_candidates=1), artifacts, RecordingModel(failed),
            adapter=CodexToolAdapter()).evolve(request)
        self.assertEqual(result.status, "error")
        self.assertEqual(result.usage.total_tokens, usage.total_tokens)

    def test_another_agent_can_reuse_the_same_evolver_with_its_own_adapter(self):
        class OtherAdapter:
            compatible_agents = ("other_agent",)
            def catalog(self, snapshot):
                return {"other_tool": ToolDescriptor(description="Another implementation", version="2")}
            def read(self, snapshot, artifacts):
                return ToolSelection.model_validate_json(artifacts.get(snapshot.artifacts["tool"]))
            def encode(self, selection):
                return selection.model_dump_json().encode()
        artifacts, request = self.request()
        request = EvolutionRequest(attempt_id=request.attempt_id, round_id=request.round_id,
            context=request.context.replace(stable_snapshot=request.context.stable_snapshot.replace(agent_plugin_id="other_agent", config={})),
            decision=request.decision, operator_id=request.operator_id, max_candidates=1, run_context=request.run_context)
        model = RecordingModel(lambda *_: response('{"enabled_tools":["other_tool"],"rationale":"test"}'))
        result = ToolSelectionEvolver(ToolSelectionEvolverConfig(max_candidates=1), artifacts, model, adapter=OtherAdapter()).evolve(request)
        self.assertEqual(result.status, "completed", result.error)
