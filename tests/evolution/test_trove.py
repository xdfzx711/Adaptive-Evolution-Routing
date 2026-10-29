import json
import unittest

from aer_core.errors import BudgetExceeded, OperationTimeout
from aer_core.schemas.evolution import EvolutionDecision, EvolutionRequest
from aer_core.schemas.usage import ResourceUsage
from plugins.evolution.trove.config import TroveConfig
from plugins.evolution.trove.evolver import TroveToolEvolver
from plugins.evolution.trove.library import ToolLibrary, read_library
from tests.evolution.helpers import evolution_context
from tests.execution.helpers import RecordingModel, response
from tests.tools.test_trove_library import path_tool


def request_for(library=None):
    state, artifacts, live = evolution_context()
    ref = artifacts.put((library or ToolLibrary()).model_dump_json().encode(), "tool")
    stable = state.stable_snapshot.replace(agent_plugin_id="codex_cli_agent",
        config={"generated_python": True, "tool_catalog": {}},
        artifacts={**state.stable_snapshot.artifacts, "tool": ref})
    return artifacts, EvolutionRequest(attempt_id="trove-attempt", round_id=1,
        context=state.replace(stable_snapshot=stable, feasible_targets=["tool", "prompt"]),
        decision=EvolutionDecision(target="tool", rationale="Choose tool capability"),
        operator_id="trove_tool_evolver", max_candidates=1, run_context=live)


def proposer(request, ctx):
    body = json.loads(request.messages[-1].content)
    if body["operation"] == "trove_select":
        eligible = next(item for item in body["candidates"] if item["mode"] == "CREATE")
        return response(json.dumps({"candidate": eligible["candidate"], "rationale": "Reusable path analysis"}))
    return response(json.dumps({"tools": [path_tool().model_dump(mode="json")] if body["mode"] == "CREATE" else [],
        "solution": None, "rationale": body["mode"]}))


class TroveEvolverTests(unittest.TestCase):
    def operator(self, artifacts, model, **config):
        return TroveToolEvolver(TroveConfig(**{"max_candidates": 1, "samples_per_mode": 1, **config}), artifacts, model)

    def test_empty_toolbox_can_create_a_library_without_modifying_parent(self):
        artifacts, request = request_for()
        stable = request.context.stable_snapshot
        model = RecordingModel(proposer)
        operator = self.operator(artifacts, model)
        self.assertTrue(operator.is_applicable(stable))
        result = operator.evolve(request)
        self.assertEqual(result.status, "completed", result.error)
        self.assertEqual(len(result.candidates), 1)
        candidate = result.candidates[0]
        self.assertEqual(set(candidate.artifact_updates), {"tool"})
        library = read_library(artifacts.get(candidate.artifact_updates["tool"]))
        self.assertEqual(library.enabled_tools, ("path_info",))
        self.assertEqual(request.context.stable_snapshot, stable)
        self.assertEqual(read_library(artifacts.get(stable.artifacts["tool"])), ToolLibrary())
        self.assertEqual(result.usage.model_calls, 3)
        self.assertEqual(result.usage.tool_calls, 1)
        receipt = json.loads(artifacts.get(result.record_ref))
        self.assertEqual([item["mode"] for item in receipt["trials"]], ["CREATE", "SKIP"])
        self.assertTrue(receipt["trials"][0]["checks"][0]["passed"])
        self.assertNotIn("stable_snapshot", json.loads(model.requests[0][0].messages[-1].content)["public_context"])

    def test_invalid_proposal_is_recorded_and_skip_preserves_library(self):
        artifacts, request = request_for()
        def generate(query, ctx):
            body = json.loads(query.messages[-1].content)
            if body["operation"] == "trove_select":
                return response('{"candidate": 2, "rationale": "No useful tool"}')
            tool = path_tool().model_dump(mode="json")
            tool["examples"][0]["expected"] = {"is_python": False}
            return response(json.dumps({"tools": [tool] if body["mode"] == "CREATE" else [], "rationale": "check"}))
        result = self.operator(artifacts, RecordingModel(generate)).evolve(request)
        self.assertEqual(result.status, "empty", result.error)
        self.assertFalse(result.candidates)
        receipt = json.loads(artifacts.get(result.record_ref))
        self.assertEqual(receipt["trials"][0]["error"], "tool_check_failed")

    def test_import_winning_is_empty_and_does_not_disable_existing_tools(self):
        library = ToolLibrary(tools=[path_tool()], enabled_tools=["path_info"])
        artifacts, request = request_for(library)
        def generate(query, ctx):
            body = json.loads(query.messages[-1].content)
            if body["operation"] == "trove_select":
                return response('{"candidate": 1, "rationale": "Reuse current tools"}')
            return response('{"tools": [], "rationale": "reuse"}')
        result = self.operator(artifacts, RecordingModel(generate)).evolve(request)
        self.assertEqual(result.status, "empty", result.error)
        self.assertEqual(read_library(artifacts.get(request.context.stable_snapshot.artifacts["tool"])), library)

    def test_execution_agreement_uses_successful_outputs_and_accounts_all_trials(self):
        artifacts, request = request_for()
        def generate(query, ctx):
            body = json.loads(query.messages[-1].content)
            self.assertEqual(body["operation"], "trove_generate")
            return response(json.dumps({"tools": [path_tool().model_dump(mode="json")] if body["mode"] == "CREATE" else [],
                "solution": "def solve():\n return {'answer': 1}", "rationale": "solution"}))
        result = self.operator(artifacts, RecordingModel(generate), selection_strategy="execution_agreement").evolve(request)
        self.assertEqual(result.status, "completed", result.error)
        self.assertEqual(result.usage.model_calls, 2)
        self.assertEqual(result.usage.tool_calls, 3)

    def test_selector_cannot_choose_failed_or_nonexistent_candidate(self):
        artifacts, request = request_for()
        def generate(query, ctx):
            body = json.loads(query.messages[-1].content)
            if body["operation"] == "trove_select":
                return response('{"candidate": 99, "rationale": "invalid"}')
            return proposer(query, ctx)
        result = self.operator(artifacts, RecordingModel(generate)).evolve(request)
        self.assertEqual(result.status, "error")
        self.assertEqual(result.usage.model_calls, 3)

    def test_agreement_majority_selects_the_matching_create_sample(self):
        artifacts, request = request_for()
        create_calls = 0
        def generate(query, ctx):
            nonlocal create_calls
            body = json.loads(query.messages[-1].content)
            is_create = body["mode"] == "CREATE"
            if is_create:
                create_calls += 1
            answer = 99 if is_create and create_calls == 1 else 1
            return response(json.dumps({"tools": [path_tool().model_dump(mode="json")] if is_create else [],
                "solution": f"def solve():\n return {{'answer': {answer}}}", "rationale": "sample"}))
        result = self.operator(artifacts, RecordingModel(generate), samples_per_mode=2,
            selection_strategy="execution_agreement").evolve(request)
        self.assertEqual(result.status, "completed", result.error)
        self.assertEqual(result.candidates[0].metadata["internal_candidate"], 2)
        self.assertEqual(result.usage.model_calls, 4)
        self.assertEqual(result.usage.tool_calls, 6)

    def test_replacing_a_tool_must_preserve_existing_dependents_checks(self):
        base = path_tool()
        child = path_tool("describe", source="def describe(path):\n return path_info(path)", dependencies=[base.name])
        library = ToolLibrary(tools=[base, child], enabled_tools=[child.name])
        artifacts, request = request_for(library)
        def generate(query, ctx):
            body = json.loads(query.messages[-1].content)
            if body["operation"] == "trove_select":
                return response('{"candidate":1,"rationale":"Keep working tools"}')
            replacement = base.replace(source="def path_info(path):\n return {'changed': True}",
                examples=[{"arguments": {"path": "x.py"}, "expected": {"changed": True}}])
            return response(json.dumps({"tools": [replacement.model_dump(mode="json")] if body["mode"] == "CREATE" else [],
                "rationale": "candidate"}))
        result = self.operator(artifacts, RecordingModel(generate)).evolve(request)
        self.assertEqual(result.status, "empty", result.error)
        trials = json.loads(artifacts.get(result.record_ref))["trials"]
        self.assertEqual(trials[1]["error"], "tool_check_failed")
        self.assertTrue(any(check["tool"] == child.name and not check["passed"] for check in trials[1]["checks"]))

    def test_provider_failures_keep_costs_and_budget_status(self):
        for failure_type, status in ((OperationTimeout, "error"), (BudgetExceeded, "budget_interrupted")):
            artifacts, request = request_for()
            usage = ResourceUsage.zero().replace(input_tokens=3, output_tokens=2, total_tokens=5,
                model_calls=1, usage_sources=["simulated"])
            def failed(*_):
                raise failure_type("fixture", stage="evolution", usage=usage)
            result = self.operator(artifacts, RecordingModel(failed)).evolve(request)
            self.assertEqual(result.status, status)
            self.assertEqual(result.usage.total_tokens, 5)
            self.assertFalse(result.candidates)

    def test_disabled_runtime_is_not_applicable(self):
        artifacts, request = request_for()
        operator = self.operator(artifacts, RecordingModel(proposer))
        self.assertFalse(operator.is_applicable(request.context.stable_snapshot.replace(config={"tool_catalog": {}})))
