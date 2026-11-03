import json
import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import httpx

from aer_core.schemas.evolution import EvolutionDecision, EvolutionRequest
from aer_core.schemas.execution import EvaluationSignal
from aer_core.schemas.usage import ResourceUsage
from plugins.agents.execution import SessionRecord
from plugins.evolution.reasoningbank.config import ReasoningBankConfig
from plugins.evolution.reasoningbank.evolver import ReasoningBankMemoryEvolver
from plugins.memory.embedding import EmbeddingProvider
from plugins.memory.library import MemoryConfig, EmbeddingConfig, decode_bank, encode_bank, parse_items
from plugins.memory.runtime import MemoryRuntime
from plugins.memory.evidence import verify_exposure
from tests.evolution.helpers import evolution_context
from tests.execution.helpers import RecordingModel, response


ITEM = "# Memory Item 1\n## Title Check assumptions\n## Description Inspect the public input contract.\n## Content AER_MEMORY_IMPROVED Verify intermediate calculations before returning the final answer."


class FakeEmbedding:
    def __init__(self):
        self.calls = []

    def embed(self, text, ctx):
        self.calls.append(text)
        vector = [0.0, 1.0] if "unrelated" in text else [1.0, 0.0]
        return vector, ResourceUsage(input_tokens=4, output_tokens=0, total_tokens=4, model_calls=1,
            tool_calls=0, elapsed_seconds=0.001, usage_complete=True, usage_sources=["simulated"])


def memory_config():
    return MemoryConfig(embedding=EmbeddingConfig(model_id="embedding-test"))


def entry(query="a useful task", items=None):
    return {"task_id": "same-task", "query": query, "status": "fail", "memory_items": items or [ITEM]}


def fixture(success=False):
    state, store, live = evolution_context()
    snapshot = state.stable_snapshot.replace(artifacts={**state.stable_snapshot.artifacts, "memory": store.put(b"", "memory")},
        config={**state.stable_snapshot.config, "memory": memory_config().controlled()})
    feedback = state.feedback.replace(success=success, score=1.0 if success else 0.0)
    signal = EvaluationSignal(**feedback.model_dump(mode="json"), evaluation_id="evaluation-1",
        execution_operation_id="exec-test", evaluator_id="benchmark-evaluator")
    state = state.replace(stable_snapshot=snapshot, feedback=feedback, evaluation_signal=signal, feasible_targets=["memory"])
    return store, EvolutionRequest(attempt_id="memory-attempt", round_id=1, context=state,
        decision=EvolutionDecision(target="memory", rationale="fixed"), operator_id="reasoningbank_memory_evolver",
        max_candidates=1, run_context=live)


class ReasoningBankTests(unittest.TestCase):
    def test_evaluation_selects_template_even_when_diagnosis_disagrees(self):
        for success in (True, False):
            store, request = fixture(success)
            request = replace(request, context=request.context.replace(diagnosis=request.context.diagnosis.replace(
                explanation="The task failed" if success else "The task succeeded")))
            model = RecordingModel(lambda *_: response(ITEM))
            operator = ReasoningBankMemoryEvolver(ReasoningBankConfig(), store, model)
            result = operator.evolve(request)
            self.assertEqual(result.status, "completed", result.error)
            prompt = model.requests[0][0].messages[0].content
            self.assertIn("successfully resolved" if success else "but failed", prompt)
            body = json.loads(model.requests[0][0].messages[1].content)
            self.assertEqual(body["evaluation_signal"]["success"], success)
            self.assertEqual(body["evaluation_signal"]["evaluator_id"], "benchmark-evaluator")
            self.assertNotEqual(body["diagnosis"], body["evaluation_signal"])
            bank = decode_bank(store.get(result.candidates[0].artifact_updates["memory"]))
            self.assertEqual(bank[0]["status"], "success" if success else "fail")
            self.assertEqual(set(bank[0]), {"task_id", "query", "status", "memory_items"})
            self.assertEqual(store.get(request.context.stable_snapshot.artifacts["memory"]), b"")
            self.assertEqual(result.usage.model_calls, 1)

    def test_missing_evaluation_is_rejected_without_model_call(self):
        store, request = fixture()
        request = replace(request, context=request.context.replace(evaluation_signal=None))
        model = RecordingModel(lambda *_: response(ITEM))
        result = ReasoningBankMemoryEvolver(ReasoningBankConfig(), store, model).evolve(request)
        self.assertEqual(result.status, "error")
        self.assertEqual(model.requests, [])

    def test_conflicting_evaluation_and_public_feedback_is_invalid(self):
        _, request = fixture()
        with self.assertRaises(ValueError):
            request.context.replace(evaluation_signal=request.context.evaluation_signal.replace(success=True))

    def test_append_preserves_prior_native_entries_and_optional_fields(self):
        store, request = fixture()
        original = [dict(entry(), think_list=["visible reasoning"], action_list=["visible action"])]
        snapshot = request.context.stable_snapshot.replace(artifacts={**request.context.stable_snapshot.artifacts,
            "memory": store.put(encode_bank(original), "memory")})
        request = replace(request, context=request.context.replace(stable_snapshot=snapshot))
        result = ReasoningBankMemoryEvolver(ReasoningBankConfig(), store, RecordingModel(lambda *_: response(ITEM))).evolve(request)
        bank = decode_bank(store.get(result.candidates[0].artifact_updates["memory"]))
        self.assertEqual(bank[0], original[0])
        self.assertEqual(len(bank), 2)
        self.assertEqual(decode_bank(store.get(snapshot.artifacts["memory"])), original)

    def test_invalid_truncated_and_empty_responses_never_create_candidates(self):
        for text, finish, status in (("[]", "stop", "empty"), ("not memory", "stop", "error"),
                (ITEM, "length", "error"), ("\n".join(ITEM.replace("Item 1", f"Item {i}") for i in range(4)), "stop", "error")):
            with self.subTest(text=text, finish=finish):
                store, request = fixture()
                result = ReasoningBankMemoryEvolver(ReasoningBankConfig(), store,
                    RecordingModel(lambda *_: response(text).replace(finish_reason=finish))).evolve(request)
                self.assertEqual(result.status, status)
                self.assertFalse(result.candidates)
                self.assertEqual(result.usage.model_calls, 1)
                self.assertIsNotNone(result.record_ref)

    def test_item_parser_keeps_markdown_and_handles_blank_lines(self):
        text = ITEM.replace("## Description", "\n## Description")
        self.assertEqual(parse_items("```markdown\n" + text + "\n```"), [text])
        self.assertEqual(decode_bank(encode_bank([entry(items=[text])]))[0]["memory_items"], [text])


class RetrievalTests(unittest.TestCase):
    def test_equal_query_prefers_latest_appended_experience(self):
        store, request = fixture()
        original = request.context.stable_snapshot
        snapshot = original.replace(artifacts={**original.artifacts, "memory": store.put(
            encode_bank([entry(items=["old lesson"]), entry(items=["new lesson"])]), "memory")})
        text, state = MemoryRuntime(memory_config(), store, embedder=FakeEmbedding()).retrieve(
            request.context.task, snapshot, request.run_context, SessionRecord())
        self.assertIn("new lesson", text)
        self.assertNotIn("old lesson", text)
        self.assertEqual(state["memory_retrieval"][0]["entry_index"], 1)

    def test_empty_bank_needs_no_embeddings(self):
        store, request = fixture()
        embedding = FakeEmbedding()
        runtime = MemoryRuntime(memory_config(), store, embedder=embedding)
        text, state = runtime.retrieve(request.context.task, request.context.stable_snapshot, request.run_context, SessionRecord())
        self.assertEqual(text, "")
        self.assertEqual(state["memory_retrieval"], [])
        self.assertEqual(embedding.calls, [])

    def test_version_membership_repeated_task_ids_cache_and_cost(self):
        store, request = fixture()
        original = request.context.stable_snapshot
        snapshot = original.replace(version_id="candidate-1", artifacts={**original.artifacts,
            "memory": store.put(encode_bank([entry("unrelated"), entry()]), "memory")})
        embedding, record = FakeEmbedding(), SessionRecord()
        runtime = MemoryRuntime(memory_config(), store, embedder=embedding)
        text, state = runtime.retrieve(request.context.task, snapshot, request.run_context, record)
        self.assertIn("AER_MEMORY_IMPROVED", text)
        self.assertEqual(state["memory_retrieval"][0]["entry_index"], 1)
        self.assertEqual(record.usage().model_calls, 3)
        cached = SessionRecord()
        runtime.retrieve(request.context.task, snapshot, request.run_context, cached)
        self.assertEqual(cached.usage().model_calls, 0)
        text, state = runtime.retrieve(request.context.task, original, request.run_context, SessionRecord())
        self.assertEqual(text, "")
        self.assertEqual(state["memory_retrieval"], [])
        self.assertEqual(len(embedding.calls), 3)

    def test_wrong_version_and_content_are_detected(self):
        store, request = fixture()
        snapshot = request.context.stable_snapshot.replace(artifacts={**request.context.stable_snapshot.artifacts,
            "memory": store.put(encode_bank([entry()]), "memory")})
        runtime = MemoryRuntime(memory_config(), store, embedder=FakeEmbedding())
        _, state = runtime.retrieve(request.context.task, snapshot, request.run_context, SessionRecord())
        execution = request.context.execution.replace(state_summary=state)
        verify_exposure(execution, snapshot, store)
        with self.assertRaises(ValueError):
            verify_exposure(execution.replace(state_summary={**state, "memory_snapshot_version_id": "wrong"}), snapshot, store)
        state["memory_retrieval"][0]["memory_items"] = ["different"]
        with self.assertRaises(ValueError):
            verify_exposure(execution.replace(state_summary=state), snapshot, store)


class EmbeddingTests(unittest.TestCase):
    @patch.dict(os.environ, {"MEMORY_EMBEDDING_API_KEY": "test-only", "MEMORY_EMBEDDING_BASE_URL": "https://embedding.invalid/v1"})
    def test_http_contract_usage_and_bad_vectors(self):
        _, request = fixture()
        for vector, ok in (([3.0, 4.0], True), ([0, 0], False), ([1.0], False), (["bad", 1], False)):
            calls = []
            def handle(req):
                calls.append(json.loads(req.content))
                return httpx.Response(200, json={"data": [{"index": 0, "embedding": vector}], "usage": {"prompt_tokens": 7}})
            provider = EmbeddingProvider(EmbeddingConfig(model_id="embedding-test", dimensions=2), transport=httpx.MockTransport(handle))
            if ok:
                result, usage = provider.embed("query", request.run_context)
                self.assertEqual(result, (0.6, 0.8))
                self.assertEqual(usage.total_tokens, 7)
            else:
                from aer_core.errors import ExecutionError
                with self.assertRaises(ExecutionError) as failure:
                    provider.embed("query", request.run_context)
                self.assertEqual(failure.exception.to_info().usage.total_tokens, 7)
            self.assertEqual(calls[0]["dimensions"], 2)


class LocalAgentMemoryTests(unittest.TestCase):
    def test_both_local_agents_inject_snapshot_memory_and_count_embedding_cost(self):
        from aer_core.config.models import AgentSettings
        from plugins.benchmarks.toy_tasks import ToyBenchmark
        from tests.execution.helpers import agent_fixture, context
        benchmark = ToyBenchmark()
        task = benchmark.task_view(benchmark.tasks("dev")[0])
        for single in (False, True):
            with self.subTest(single=single):
                model = RecordingModel(lambda *_: response('{"answer":75}'))
                original, snapshot, store = agent_fixture(single=single, model=model)
                config = memory_config()
                snapshot = snapshot.replace(config={**snapshot.model_dump(mode="json")["config"], "memory": config.controlled()},
                    artifacts={**snapshot.artifacts, "memory": store.put(encode_bank([entry()]), "memory")})
                settings = AgentSettings(plugin=original.plugin_id, initial_prompt="fixture", initial_snapshot_ref=None,
                    max_steps=snapshot.config["max_steps"], config={"memory": config.model_dump(mode="json")})
                agent = type(original)(settings, snapshot, store, model, original.tools)
                agent.memory.embedder = FakeEmbedding()
                result = agent.run(task, snapshot, context())
                self.assertEqual(result.status, "completed", result.error)
                self.assertIn("AER_MEMORY_IMPROVED", model.requests[0][0].messages[0].content)
                self.assertEqual(result.usage.total_tokens, 13)
                self.assertEqual(result.usage.model_calls, 3)
                verify_exposure(result, snapshot, store)
