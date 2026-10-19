import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from aer_core.schemas.execution import ExecutionView, TraceEvent
from aer_core.schemas.usage import ResourceUsage
from experiments.reporting import summarize_saved_run
from experiments.round_report import RoundReport, execution_view
from plugins.diagnosers.llm import LLMDiagnoser
from plugins.storage._files import FileRoot, canonical_bytes
from tests.execution.helpers import RecordingModel, response
from tests.orchestration.helpers import attach_model, configured


def records(root):
    return sorted(root.glob("rounds/*/round-*/record.json"))


class RoundReportTests(unittest.TestCase):
    def test_rounds_inline_full_candidates_and_paired_results_without_machine_ids(self):
        with tempfile.TemporaryDirectory() as directory:
            _, startup = configured(Path(directory), rounds=2)
            task = startup.prepare_task("dev-001")
            attach_model(task, always_wrong=True)
            result = task.run()
            root = startup.execution.storage.run_root
            paths = records(root)
            self.assertEqual([path.parent.name for path in paths], ["round-001", "round-002"])
            self.assertEqual(len(paths), result.evolution_rounds_used)
            for number, path in enumerate(paths, 1):
                text = path.read_text(encoding="utf-8")
                data = json.loads(text)
                self.assertTrue(text.startswith('{\n  "round":'))
                self.assertEqual(len(list(path.parent.glob("*.json"))), 1)
                for technical in ('"candidate_ids"', '"artifact_id"', '"operation_id"', '"attempt_id"',
                                  '"config_schema"', '"scoring_specs"', '"private_parameters"', 'sha256-', '.bin'):
                    self.assertNotIn(technical, text)
                candidate = data["candidates"][0]
                self.assertIn(f"round {number}", candidate["components"]["prompt"])
                self.assertEqual(candidate["status"], "rejected")
                self.assertFalse(candidate["committed"])
                pairs = candidate["validation"]["tasks"]
                self.assertEqual({pair["group"] for pair in pairs}, {"local", "similar", "regression"})
                self.assertEqual(len(pairs), 9)
                self.assertEqual(pairs[0]["baseline"]["execution"]["output"], {"answer": -999})
                self.assertIsNotNone(pairs[0]["input"])
            second = json.loads(paths[1].read_text(encoding="utf-8"))
            self.assertEqual(second["input_to_evolver"]["history"][0]["round"], 1)
            self.assertEqual(second["run_result"]["stop_reason"], "max_evolution_rounds_reached")
            text_report = (root / "evolution.txt").read_text(encoding="utf-8")
            self.assertIn(str(paths[0]), text_report)
            self.assertNotIn("报告异常", text_report)

    def test_multiple_candidates_keep_acceptance_selection_commit_and_retest_distinct(self):
        with tempfile.TemporaryDirectory() as directory:
            _, startup = configured(Path(directory), rounds=1, candidates=2)
            task = startup.prepare_task("dev-001")
            attach_model(task, candidates=2)
            result = task.run()
            data = json.loads(records(startup.execution.storage.run_root)[0].read_text(encoding="utf-8"))
            candidates = data["candidates"]
            self.assertTrue(all(candidate["acceptance"]["accepted"] for candidate in candidates))
            self.assertEqual(sum(candidate["selected"] for candidate in candidates), 1)
            self.assertEqual(sum(candidate["committed"] for candidate in candidates), 1)
            self.assertEqual(data["post_commit_execution"]["execution"]["output"], dict(result.final_execution.output))
            self.assertTrue(data["post_commit_execution"]["evaluation"]["success"])

    def test_empty_error_and_incomplete_validation_are_still_recorded(self):
        cases = [({"empty": True}, "empty_candidates"), ({"generation_error": True}, "evolution_error"),
                 ({"unknown_validation": True}, "validation_error")]
        for options, expected in cases:
            with self.subTest(options=options), tempfile.TemporaryDirectory() as directory:
                _, startup = configured(Path(directory), rounds=1)
                task = startup.prepare_task("dev-001")
                attach_model(task, **options)
                task.run()
                data = json.loads(records(startup.execution.storage.run_root)[0].read_text(encoding="utf-8"))
                self.assertEqual(data["status"], expected)
                self.assertTrue(all(not candidate["committed"] for candidate in data["candidates"]))

    def test_failed_diagnosis_has_a_round_record_even_without_a_generated_candidate(self):
        with tempfile.TemporaryDirectory() as directory:
            _, startup = configured(Path(directory), rounds=1)
            task = startup.prepare_task("dev-001")
            attach_model(task, always_wrong=True)
            task.controller.diagnoser = LLMDiagnoser(RecordingModel(lambda *_: response("bad JSON")),
                                                    startup.execution.storage.artifact_store)
            result = task.run()
            data = json.loads(records(startup.execution.storage.run_root)[0].read_text(encoding="utf-8"))
            self.assertEqual(result.stop_reason, "diagnosis_error")
            self.assertEqual(result.evolution_rounds_used, 0)
            self.assertEqual(data["status"], "diagnosis_error")
            self.assertEqual(data["diagnosis"]["response"]["content"], "bad JSON")
            self.assertEqual(data["candidates"], [])

    def test_offline_export_rebuilds_same_round_contents_without_touching_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            _, startup = configured(Path(directory), rounds=2)
            task = startup.prepare_task("dev-001")
            attach_model(task, always_wrong=True)
            result = task.run()
            root = startup.execution.storage.run_root
            originals = [json.loads(path.read_text(encoding="utf-8")) for path in records(root)]
            paths = records(root)
            for path in paths:
                path.unlink()
            (root / "evolution.txt").unlink()
            source = Path(directory) / "result.json"
            source.write_text(json.dumps(startup.save_record(result)), encoding="utf-8")
            events_path = root / "experience/events.jsonl"
            original_events = events_path.read_bytes()
            with patch.object(task.execution.agent.model, "generate", side_effect=AssertionError("offline only")):
                summarize_saved_run(source)
            rebuilt = [json.loads(path.read_text(encoding="utf-8")) for path in records(root)]
            self.assertEqual(len(rebuilt), 2)
            for old, new in zip(originals, rebuilt):
                for key in ("candidates", "routing", "input_to_evolver", "run_result", "status"):
                    self.assertEqual(old.get(key), new.get(key))
            self.assertEqual(events_path.read_bytes(), original_events)

    def test_repeated_tasks_use_separate_time_folders_and_no_evolution_creates_none(self):
        with tempfile.TemporaryDirectory() as directory:
            _, startup = configured(Path(directory), rounds=1)
            for _ in range(2):
                task = startup.prepare_task("dev-001")
                attach_model(task, always_wrong=True)
                task.run()
            root = startup.execution.storage.run_root
            self.assertEqual(len({path.parent.parent for path in records(root)}), 2)
            task = startup.prepare_task("dev-002")
            attach_model(task, initial_success=True)
            task.run()
            self.assertEqual(len(records(root)), 2)

    def test_offline_export_places_commit_retest_in_its_own_round(self):
        with tempfile.TemporaryDirectory() as directory:
            _, startup = configured(Path(directory), rounds=2)
            task = startup.prepare_task("dev-001")
            attach_model(task, local_failure=True)
            result = task.run()
            root = startup.execution.storage.run_root
            old = [json.loads(path.read_text(encoding="utf-8")) for path in records(root)]
            self.assertTrue(old[0]["candidates"][0]["committed"])
            self.assertIn("post_commit_execution", old[0])
            self.assertNotIn("post_commit_execution", old[1])
            for path in records(root):
                path.unlink()
            (root / "evolution.txt").unlink()
            source = Path(directory) / "result.json"
            source.write_text(json.dumps(startup.save_record(result)), encoding="utf-8")
            summarize_saved_run(source)
            rebuilt = [json.loads(path.read_text(encoding="utf-8")) for path in records(root)]
            for before, after in zip(old, rebuilt):
                for key in ("candidates", "post_commit_execution", "evaluation_before_evolution", "input_to_evolver", "status"):
                    self.assertEqual(before.get(key), after.get(key))

    def test_business_ids_and_generic_component_content_are_preserved(self):
        execution = ExecutionView(task_id="task", agent_version_id="technical-version", output={"id": 10},
            trajectory=[TraceEvent(seq=1, kind="tool_result", payload={"call_id": "technical-call",
                "output": {"candidate_id": "business-value"}})], state_summary={}, usage=ResourceUsage.zero())
        readable = execution_view(execution)
        self.assertEqual(readable["output"]["id"], 10)
        self.assertEqual(readable["trace"][0]["result"]["output"]["candidate_id"], "business-value")
        self.assertNotIn("call_id", readable["trace"][0]["result"])
        with tempfile.TemporaryDirectory() as directory:
            _, startup = configured(Path(directory), rounds=1)
            task = startup.prepare_task("dev-001")
            artifacts = startup.execution.storage.artifact_store
            report = RoundReport(startup.execution.storage.run_root, task.task, task.context, artifacts)
            memory = artifacts.put(b'{"items":[{"id":7,"text":"experience"}]}', "memory")
            self.assertEqual(report.content(memory), {"items": [{"id": 7, "text": "experience"}]})
            binary = artifacts.put(b"\xff\x00", "binary_component")
            self.assertEqual(report.content(binary), {"encoding": "base64", "content": "/wA="})

    def test_pretty_storage_json_keeps_legacy_immutability_and_distinguishes_values(self):
        from aer_core.errors import StorageError
        with tempfile.TemporaryDirectory() as directory:
            files = FileRoot(directory)
            data = {"values": [1, {"meaning": "中文"}]}
            files.immutable("new.json", canonical_bytes(data))
            self.assertIn('\n  "values": [\n', files.read("new.json").decode("utf-8"))
            files.path("old.json").write_bytes(canonical_bytes(data))
            original = files.read("old.json")
            files.immutable("old.json", files.read("new.json"))
            self.assertEqual(files.read("old.json"), original)
            files.immutable("typed.json", canonical_bytes({"value": 1}))
            with self.assertRaises(StorageError):
                files.immutable("typed.json", canonical_bytes({"value": True}))


if __name__ == "__main__":
    unittest.main()
