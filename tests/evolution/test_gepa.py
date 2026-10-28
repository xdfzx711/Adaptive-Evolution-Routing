import importlib.util
from dataclasses import replace
import unittest
from unittest.mock import patch

from aer_core.errors import EvolutionError, ExecutionError, PluginConfigurationError
from aer_core.schemas.evolution import EvolutionDecision, EvolutionRequest
from aer_core.schemas.execution import ExecutionView, FeedbackView
from aer_core.schemas.optimization import OptimizationRecord, TrialObservation
from aer_core.schemas.usage import ResourceUsage
from plugins.evolution.gepa.config import GEPAPromptConfig
from plugins.evolution.gepa.adapter import AERGEPAAdapter
from plugins.evolution.gepa.evolver import GEPAPromptEvolver, require_gepa
from plugins.evolution.gepa.recording import SearchLimit, SearchRecord
from tests.evolution.helpers import evolution_context
from tests.execution.helpers import RecordingModel, response


class Trials:
    def __init__(self, fail=False):
        self.calls, self.fail = [], fail

    def tasks(self, current_task, task_ids):
        return (current_task,)

    def evaluate(self, current_task, task, snapshot, prompt, run_ctx):
        self.calls.append((snapshot, prompt, run_ctx))
        if self.fail:
            raise ExecutionError("fixture", stage="evolution", usage=ResourceUsage.zero())
        passed = "IMPROVED" in prompt
        return TrialObservation(task=self.public, execution=ExecutionView(task_id=task.task_id,
            agent_version_id=snapshot.version_id, output={"answer": float(passed)}, trajectory=[],
            state_summary={}, usage=ResourceUsage.zero()),
            feedback=FeedbackView(score=float(passed), success=passed, reason_code="correct" if passed else "incorrect_answer",
                                  public_feedback="Public task feedback", evidence_step_ids=[]), usage=ResourceUsage.zero())


def setup(**changes):
    state, artifacts, live = evolution_context()
    request = EvolutionRequest(attempt_id="gepa-attempt", round_id=1, context=state,
        decision=EvolutionDecision(target="prompt", rationale="test"), operator_id="gepa_prompt_evolver",
        max_candidates=1, run_context=live)
    trials = Trials()
    trials.public = state.task
    model = RecordingModel(lambda *_: response("IMPROVED reusable instructions"))
    config = GEPAPromptConfig(max_candidates=1, max_metric_calls=8, max_iterations=2, **changes)
    return request, artifacts, trials, model, config


@unittest.skipUnless(importlib.util.find_spec("gepa"), "install the [gepa] extra")
class GEPATests(unittest.TestCase):
    def test_three_samples_are_reused_for_reflection_and_selection(self):
        request, _, trials, _, config = setup()
        record = SearchRecord("seed", config)
        adapter = AERGEPAAdapter(trials, request, record)
        evaluate = trials.evaluate
        values = iter([0.2, 0.5, 0.8])

        def varied(*args):
            observed = evaluate(*args)
            value = next(values)
            return observed.replace(execution=observed.execution.replace(output={"answer": value}),
                feedback=observed.feedback.replace(score=value))

        trials.evaluate = varied
        baseline = adapter.evaluate([adapter.current_task], {"prompt": "seed"})
        parent = adapter.evaluate([adapter.current_task], {"prompt": "seed"}, capture_traces=True)
        selection = adapter.evaluate([adapter.current_task], {"prompt": "seed"})
        self.assertEqual(len(trials.calls), 3)
        self.assertEqual([call[2].seed for call in trials.calls], [request.run_context.seed + i for i in range(3)])
        self.assertEqual([baseline.num_metric_calls, parent.num_metric_calls, selection.num_metric_calls], [3, 0, 0])
        self.assertEqual(baseline.scores, [0.5])
        self.assertEqual(parent.scores, baseline.scores)
        self.assertEqual(selection.scores, baseline.scores)
        data = adapter.make_reflective_dataset({"prompt": "seed"}, parent, ["prompt"])["prompt"]
        self.assertEqual([item["Feedback"]["score"] for item in data], [0.2, 0.5, 0.8])
        self.assertEqual([item["Generated Outputs"]["answer"] for item in data], [0.2, 0.5, 0.8])
        self.assertTrue(all(item["Repeated Evaluation"]["mean_score"] == 0.5 for item in data))
        # The cached group cannot be changed by a consumer of returned traces.
        parent.trajectories[0]["samples"][0]["feedback"]["score"] = 1.0
        again = adapter.evaluate([adapter.current_task], {"prompt": "seed"}, capture_traces=True)
        self.assertEqual(again.trajectories[0]["samples"][0]["feedback"]["score"], 0.2)

    def test_cache_distinguishes_prompt_task_snapshot_and_execution_conditions(self):
        for change in ("prompt", "task", "snapshot", "context"):
            with self.subTest(change=change):
                request, _, trials, _, config = setup()
                record = SearchRecord("seed", config)
                adapter = AERGEPAAdapter(trials, request, record)
                task, prompt = adapter.current_task, "seed"
                adapter.evaluate([task], {"prompt": prompt})
                if change == "prompt":
                    prompt = "different"
                elif change == "task":
                    task = task.replace(task_id="different-task")
                elif change == "snapshot":
                    adapter.request = replace(request, context=request.context.replace(
                        stable_snapshot=request.context.stable_snapshot.replace(version_id="different-version"),
                        execution=request.context.execution.replace(agent_version_id="different-version")))
                else:
                    adapter.request = replace(request, run_context=replace(request.run_context, max_output_tokens=17))
                adapter.evaluate([task], {"prompt": prompt})
                self.assertEqual(len(trials.calls), 6)

    def test_cache_does_not_cross_searches_and_can_be_read_at_metric_limit(self):
        request, _, trials, _, config = setup()
        config = config.model_copy(update={"max_metric_calls": 3})
        for _ in range(2):
            record = SearchRecord("seed", config)
            adapter = AERGEPAAdapter(trials, request, record)
            first = adapter.evaluate([adapter.current_task], {"prompt": "seed"})
            again = adapter.evaluate([adapter.current_task], {"prompt": "seed"}, capture_traces=True)
            self.assertEqual(first.scores, again.scores)
            self.assertEqual(again.num_metric_calls, 0)
            self.assertEqual(len(record.trials), 3)
        self.assertEqual(len(trials.calls), 6)

    def test_eight_metric_budget_stops_after_two_complete_groups(self):
        request, artifacts, trials, model, config = setup()
        result = GEPAPromptEvolver(config, artifacts, model, trials).evolve(request)
        receipt = OptimizationRecord.model_validate_json(artifacts.get(result.record_ref))
        self.assertEqual(result.status, "completed", result.error)
        self.assertEqual(len(trials.calls), 6)
        self.assertEqual(len(model.requests), 1)
        self.assertEqual(receipt.stop_reason, "metric_limit")
        groups = [item for item in receipt.decisions if item["event"] == "sampled_evaluation"]
        self.assertEqual([item["sample_count"] for item in groups], [3, 3])
        self.assertEqual(groups[0]["sample_seeds"], groups[1]["sample_seeds"])
        # Candidate full-selection ranking reuses its initial sample group.
        self.assertTrue(any(item["event"] == "evaluation_reused" and item["candidate"] == 2
                            for item in receipt.decisions))
        self.assertEqual(next(item.score for item in receipt.candidates if item.selected), 1.0)

    def test_optimizer_compares_means_instead_of_best_rollouts(self):
        request, artifacts, trials, model, config = setup()
        evaluate = trials.evaluate
        values = iter([0.6, 0.6, 0.6, 1.0, 0.1, 0.1])

        def varied(*args):
            observed = evaluate(*args)
            return observed.replace(feedback=observed.feedback.replace(score=next(values)))

        trials.evaluate = varied
        result = GEPAPromptEvolver(config, artifacts, model, trials).evolve(request)
        receipt = OptimizationRecord.model_validate_json(artifacts.get(result.record_ref))
        self.assertEqual(result.status, "empty", result.error)
        self.assertEqual(receipt.reason, "best_is_seed")
        rejected = next(item for item in receipt.decisions if item["event"] == "rejected_in_search")
        self.assertAlmostEqual(rejected["baseline_score"], 0.6)
        self.assertAlmostEqual(rejected["candidate_score"], 0.4)
        self.assertEqual(len(trials.calls), 6)

    def test_partial_sample_failure_keeps_costs_and_never_ranks_partial_group(self):
        request, artifacts, trials, model, config = setup()
        evaluate = trials.evaluate
        usage = ResourceUsage(input_tokens=2, output_tokens=1, total_tokens=3, model_calls=1,
                              tool_calls=0, elapsed_seconds=0, usage_complete=True, usage_sources=["simulated"])

        def failing(*args):
            observed = evaluate(*args)
            if len(trials.calls) == 5:
                raise ExecutionError("sample two failed", stage="evolution", usage=usage)
            return observed.replace(usage=usage)

        trials.evaluate = failing
        result = GEPAPromptEvolver(config, artifacts, model, trials).evolve(request)
        receipt = OptimizationRecord.model_validate_json(artifacts.get(result.record_ref))
        self.assertEqual(result.status, "error")
        self.assertEqual(result.candidates, ())
        self.assertEqual(len(receipt.trials), 5)
        self.assertEqual(receipt.usage.total_tokens, 5 * 3 + 5)  # All samples plus one reflection.
        self.assertIsNotNone(receipt.trials[-1].observation.error)
        self.assertIsNone(receipt.candidates[-1].score)
        groups = [item for item in receipt.decisions if item["event"] == "sampled_evaluation"]
        self.assertEqual([item["candidate"] for item in groups], [1])

    def test_batch_budget_is_checked_before_starting_incomplete_groups(self):
        request, _, trials, _, config = setup()
        config = config.model_copy(update={"max_metric_calls": 5})
        record = SearchRecord("seed", config)
        adapter = AERGEPAAdapter(trials, request, record)
        with self.assertRaises(SearchLimit):
            adapter.evaluate([adapter.current_task, adapter.current_task.replace(task_id="another")], {"prompt": "seed"})
        self.assertEqual(trials.calls, [])
        self.assertEqual(record.stop_reason, "metric_limit")

    def test_timeout_does_not_publish_partial_sample_average(self):
        request, artifacts, trials, model, config = setup()
        evaluate = trials.evaluate
        clock = [0.0]

        def slow(*args):
            observed = evaluate(*args)
            if len(trials.calls) == 2:
                clock[0] = config.timeout_seconds + 1
            return observed

        trials.evaluate = slow
        with patch("plugins.evolution.gepa.recording.monotonic", side_effect=lambda: clock[0]):
            result = GEPAPromptEvolver(config, artifacts, model, trials).evolve(request)
        receipt = OptimizationRecord.model_validate_json(artifacts.get(result.record_ref))
        self.assertEqual(result.status, "empty", result.error)
        self.assertEqual(result.candidates, ())
        self.assertEqual(receipt.stop_reason, "timeout")
        self.assertEqual(len(receipt.trials), 2)
        self.assertTrue(all(item.score is None for item in receipt.candidates))
        self.assertFalse(any(item["event"] == "sampled_evaluation" for item in receipt.decisions))

    def test_different_selection_tasks_execute_their_own_samples(self):
        request, _, trials, _, config = setup()
        record = SearchRecord("seed", config)
        adapter = AERGEPAAdapter(trials, request, record)
        train = adapter.current_task
        selection = train.replace(task_id="selection-task")
        adapter.evaluate([train], {"prompt": "seed"})
        adapter.evaluate([train, selection], {"prompt": "seed"})
        self.assertEqual(len(trials.calls), 6)
        self.assertEqual(len(adapter.evaluations), 2)

    def test_cross_round_duplicate_is_regenerated_before_trial(self):
        request, artifacts, trials, _, config = setup()
        config = config.model_copy(update={"max_iterations": 1})
        outputs = iter(["IMPROVED first", "IMPROVED first", "IMPROVED different"])
        model = RecordingModel(lambda *_: response(next(outputs)))
        evolver = GEPAPromptEvolver(config, artifacts, model, trials)
        first = evolver.evolve(request)
        previous_calls = len(trials.calls)
        second = evolver.evolve(replace(request, round_id=2, attempt_id="gepa-second",
            context=request.context.replace(round_id=2)))
        self.assertEqual(first.status, "completed")
        self.assertEqual(second.status, "completed", second.error)
        self.assertEqual(artifacts.get(second.candidates[0].artifact_updates["prompt"]), b"IMPROVED different")
        self.assertNotIn("IMPROVED first", [prompt for _, prompt, _ in trials.calls[previous_calls:]])
        receipt = OptimizationRecord.model_validate_json(artifacts.get(second.record_ref))
        self.assertEqual(len(receipt.reflections), 2)
        self.assertEqual(second.usage.model_calls, 2)
        duplicate = next(item for item in receipt.decisions if item["event"] == "duplicate_prompt")
        self.assertEqual(duplicate["first_seen_round"], 1)
        self.assertEqual(duplicate["action"], "regenerate")
        self.assertIn("outer round 1", model.requests[-1][0].messages[-1].content)

    def test_repeated_duplicate_ends_search_without_retrying_the_task(self):
        request, artifacts, trials, model, config = setup()
        config = config.model_copy(update={"max_iterations": 1})
        evolver = GEPAPromptEvolver(config, artifacts, model, trials)
        self.assertEqual(evolver.evolve(request).status, "completed")
        previous_calls = len(trials.calls)
        previous_reflections = len(model.requests)
        second = evolver.evolve(replace(request, round_id=2, attempt_id="gepa-second",
            context=request.context.replace(round_id=2)))
        receipt = OptimizationRecord.model_validate_json(artifacts.get(second.record_ref))
        self.assertEqual(second.status, "empty", receipt.error)
        self.assertEqual(receipt.reason, "duplicate_prompt")
        self.assertEqual(receipt.stop_reason, "duplicate_prompt")
        self.assertIsNone(receipt.error)
        self.assertEqual(len(model.requests) - previous_reflections, 2)
        self.assertEqual(second.usage.model_calls, 2)
        self.assertTrue(all("IMPROVED" not in prompt for _, prompt, _ in trials.calls[previous_calls:]))

    def test_new_tool_configuration_can_retry_the_same_prompt(self):
        request, artifacts, trials, model, config = setup()
        config = config.model_copy(update={"max_iterations": 1})
        evolver = GEPAPromptEvolver(config, artifacts, model, trials)
        self.assertEqual(evolver.evolve(request).status, "completed")
        old = request.context.stable_snapshot
        updated = old.replace(version_id="new-tool-version", parent_version_id=old.version_id,
            artifacts={**old.artifacts, "tool": artifacts.put(b'{"enabled_tools":["different"]}', "tool")})
        context = request.context.replace(round_id=2, stable_snapshot=updated,
            execution=request.context.execution.replace(agent_version_id=updated.version_id))
        second = evolver.evolve(replace(request, round_id=2, attempt_id="gepa-second", context=context))
        self.assertEqual(second.status, "completed", second.error)
        receipt = OptimizationRecord.model_validate_json(artifacts.get(second.record_ref))
        self.assertFalse(any(item["event"] == "duplicate_prompt" for item in receipt.decisions))

    def test_nine_metrics_complete_two_improving_internal_candidates(self):
        request, artifacts, trials, _, config = setup()
        config = config.model_copy(update={"max_metric_calls": 9})
        outputs = iter(["IMPROVED first", "IMPROVED second"])
        model = RecordingModel(lambda *_: response(next(outputs)))
        evaluate = trials.evaluate
        def graded(*args):
            observed = evaluate(*args)
            value = {"IMPROVED first": 0.5, "IMPROVED second": 1.0}.get(args[3], 0.0)
            return observed.replace(feedback=observed.feedback.replace(score=value, success=value == 1,
                reason_code="correct" if value == 1 else "incorrect_answer"))
        trials.evaluate = graded
        result = GEPAPromptEvolver(config, artifacts, model, trials).evolve(request)
        receipt = OptimizationRecord.model_validate_json(artifacts.get(result.record_ref))
        self.assertEqual(result.status, "completed", receipt.error)
        self.assertEqual(artifacts.get(result.candidates[0].artifact_updates["prompt"]), b"IMPROVED second")
        self.assertEqual(len(receipt.trials), 9)
        self.assertEqual(next(item for item in receipt.candidates if item.selected).score, 1.0)

    def test_real_optimizer_exports_prompt_and_accounts_for_every_reflection(self):
        request, artifacts, trials, model, config = setup()
        before = request.context.stable_snapshot
        result = GEPAPromptEvolver(config, artifacts, model, trials).evolve(request)
        receipt = OptimizationRecord.model_validate_json(artifacts.get(result.record_ref))
        self.assertEqual(result.status, "completed", receipt.error)
        self.assertEqual(len(result.candidates), 1)
        candidate = result.candidates[0]
        self.assertEqual(candidate.parent_version_id, before.version_id)
        self.assertEqual(set(candidate.artifact_updates), {"prompt"})
        self.assertEqual(artifacts.get(candidate.artifact_updates["prompt"]), b"IMPROVED reusable instructions")
        self.assertEqual(request.context.stable_snapshot, before)
        self.assertTrue(all(snapshot == before for snapshot, _, _ in trials.calls))
        self.assertEqual(result.usage.model_calls, len(model.requests))
        self.assertEqual(len(receipt.reflections), len(model.requests))
        self.assertEqual(len(receipt.trials), len(trials.calls))
        self.assertLessEqual(len(trials.calls), config.max_metric_calls)
        self.assertEqual(receipt.engine_version, "0.1.4")
        self.assertEqual(sum(item.selected for item in receipt.candidates), 1)
        self.assertTrue(receipt.config["selection_reuses_training"])
        self.assertEqual(receipt.usage, result.usage)

    def test_empty_search_retains_receipt_and_reason(self):
        request, artifacts, trials, model, config = setup()
        config = config.model_copy(update={"max_metric_calls": 3})
        result = GEPAPromptEvolver(config, artifacts, model, trials).evolve(request)
        receipt = OptimizationRecord.model_validate_json(artifacts.get(result.record_ref))
        self.assertEqual(result.status, "empty", receipt.error)
        self.assertEqual(result.candidates, ())
        self.assertEqual(receipt.reason, "best_is_seed")
        self.assertEqual(model.requests, [])
        self.assertEqual(len(receipt.trials), 3)

    def test_trial_failure_is_not_a_zero_score_and_retains_costs(self):
        request, artifacts, trials, model, config = setup()
        trials.fail = True
        result = GEPAPromptEvolver(config, artifacts, model, trials).evolve(request)
        receipt = OptimizationRecord.model_validate_json(artifacts.get(result.record_ref))
        self.assertEqual(result.status, "error")
        self.assertIsNone(receipt.trials[0].observation.feedback)
        self.assertIsNotNone(receipt.trials[0].observation.error)
        self.assertEqual(model.requests, [])

    def test_reflection_failure_retains_call_and_usage(self):
        request, artifacts, trials, model, config = setup()
        model = RecordingModel(lambda *_: response("truncated").replace(finish_reason="length"))
        result = GEPAPromptEvolver(config, artifacts, model, trials).evolve(request)
        receipt = OptimizationRecord.model_validate_json(artifacts.get(result.record_ref))
        self.assertEqual(result.status, "error")
        self.assertEqual(result.usage.model_calls, 1)
        self.assertEqual(result.usage.total_tokens, 5)
        self.assertEqual(len(receipt.reflections), 1)
        self.assertIsNotNone(receipt.reflections[0].error)

    def test_invalid_provider_response_has_unknown_usage_and_is_not_retried(self):
        request, artifacts, trials, model, config = setup()
        model = RecordingModel(lambda *_: {"invalid": "response"})
        result = GEPAPromptEvolver(config, artifacts, model, trials).evolve(request)
        receipt = OptimizationRecord.model_validate_json(artifacts.get(result.record_ref))
        self.assertEqual(result.status, "error")
        self.assertEqual(len(model.requests), 1)
        self.assertFalse(result.usage.usage_complete)
        self.assertIsNone(receipt.reflections[0].response)

    def test_small_metric_limits_do_not_export_unvalidated_internal_children(self):
        for limit in (2, 3):
            with self.subTest(limit=limit):
                request, artifacts, trials, model, config = setup()
                result = GEPAPromptEvolver(config.model_copy(update={"max_metric_calls": limit}), artifacts, model, trials).evolve(request)
                receipt = OptimizationRecord.model_validate_json(artifacts.get(result.record_ref))
                self.assertEqual(result.status, "empty")
                self.assertLessEqual(len(receipt.trials), limit)
                self.assertEqual(receipt.stop_reason, "metric_limit")

    def test_missing_optional_dependency_is_a_clear_setup_error(self):
        with patch("plugins.evolution.gepa.evolver.version", return_value="different"):
            with self.assertRaisesRegex(PluginConfigurationError, "gepa==0.1.4"):
                require_gepa()

    def test_receipt_storage_failure_preserves_already_consumed_usage(self):
        request, artifacts, trials, model, config = setup()
        put = artifacts.put
        def fail_receipt(content, kind):
            if kind == "evolution_optimization":
                raise RuntimeError("receipt unavailable")
            return put(content, kind)
        with patch.object(artifacts, "put", fail_receipt):
            with self.assertRaises(EvolutionError) as failure:
                GEPAPromptEvolver(config, artifacts, model, trials).evolve(request)
        self.assertEqual(failure.exception.to_info().usage.model_calls, len(model.requests))
        self.assertGreater(failure.exception.to_info().usage.total_tokens, 0)
