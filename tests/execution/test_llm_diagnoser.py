import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import yaml

from aer_core.errors import DiagnosisError, OperationTimeout
from aer_core.schemas.diagnosis import DiagnosisRecord
from aer_core.schemas.execution import ExecutionView, FeedbackView, TraceEvent
from aer_core.schemas.usage import ResourceUsage
from experiments.execution import prepare_execution
from experiments.orchestration import prepare_orchestrator
from plugins.benchmarks.toy_tasks import ToyBenchmark
from plugins.diagnosers.llm import LLMDiagnoser
from plugins.mocks import MockArtifactStore
from tests.execution.helpers import RecordingModel, context, response
from tests.orchestration.helpers import attach_model, configured


def analysis_text(**changes):
    data = {"reason_code": "incorrect_answer", "explanation": "公开结果与反馈表明答案未通过，需要核对执行步骤。",
        "observations": [{"text": "最终响应已生成，但外部评分未通过。", "evidence_step_ids": [1]}],
        "hypotheses": [{"text": "可能遗漏了题目中的某条计算规则，尚待验证。", "evidence_step_ids": [1]}],
        "evidence_step_ids": [1], "confidence": None}
    data.update(changes)
    return json.dumps(data, ensure_ascii=False)


class LLMDiagnoserTests(unittest.TestCase):
    def fixture(self, answer=None):
        task = ToyBenchmark().task_view(ToyBenchmark().tasks("dev")[0])
        execution = ExecutionView(task_id=task.task_id, agent_version_id="v0", output={"answer": -1},
            trajectory=[TraceEvent(seq=1, kind="model_response", payload={"content": '{"answer":-1}'})],
            state_summary={"termination_reason": "finish"}, usage=ResourceUsage.zero())
        feedback = FeedbackView(score=0, success=False, reason_code="incorrect_answer", public_feedback="Answer incorrect.")
        store = MockArtifactStore()
        model = RecordingModel(answer or (lambda req, ctx: response(analysis_text())))
        return LLMDiagnoser(model, store), model, store, task, execution, feedback, context(stage="diagnosis")

    def test_model_receipt_and_evidence_are_recorded_without_selecting_target(self):
        diagnoser, model, store, task, execution, feedback, ctx = self.fixture()
        result = diagnoser.diagnose(task, execution, feedback, ctx)
        self.assertEqual(result.source, "model")
        self.assertEqual(result.usage.total_tokens, 5)
        self.assertEqual(result.all_evidence_step_ids, {1})
        self.assertEqual(len(result.observations), 1)
        self.assertEqual(len(result.hypotheses), 1)
        payload = json.loads(model.requests[0][0].messages[-1].content)
        self.assertEqual(payload["operation"], "failure_diagnosis")
        self.assertEqual(model.requests[0][0].tools, ())
        self.assertNotIn("target", result.model_dump())
        receipt = DiagnosisRecord.model_validate_json(store.get(result.record_ref))
        self.assertEqual(receipt.feedback, feedback)
        self.assertEqual(receipt.usage, result.usage)
        self.assertIsNone(receipt.diagnosis.record_ref)

    def test_invalid_json_invalid_nested_evidence_and_truncation_keep_actual_usage(self):
        cases = [response("not JSON"), response(analysis_text(reason_code="no_failure")), response(analysis_text(evidence_step_ids=[99])),
            response(analysis_text(hypotheses=[{"text": "未知证据", "evidence_step_ids": [99]}])),
            response(analysis_text()).replace(finish_reason="length")]
        for value in cases:
            with self.subTest(value=value.content):
                diagnoser, _, store, task, execution, feedback, ctx = self.fixture(lambda *_: value)
                with self.assertRaises(DiagnosisError) as caught:
                    diagnoser.diagnose(task, execution, feedback, ctx)
                self.assertEqual(caught.exception.to_info().usage.total_tokens, 5)
                receipt = DiagnosisRecord.model_validate_json(store.get(caught.exception.to_info().evidence_ref))
                self.assertIsNone(receipt.diagnosis)
                self.assertIsNotNone(receipt.error)

    def test_provider_errors_and_measured_timeout_keep_call_usage(self):
        actual = ResourceUsage(input_tokens=100, output_tokens=20, total_tokens=120, model_calls=1,
            tool_calls=0, elapsed_seconds=1, usage_complete=True, usage_sources=["provider_reported"])
        for failure, expected in ((OperationTimeout("fixture timeout", stage="diagnosis", usage=actual), 120),
                                  (RuntimeError("fixture disconnected"), None)):
            def fail(*_, error=failure):
                raise error
            diagnoser, _, _, task, execution, feedback, ctx = self.fixture(fail)
            with self.assertRaises(DiagnosisError) as caught:
                diagnoser.diagnose(task, execution, feedback, ctx)
            self.assertEqual(caught.exception.to_info().usage.total_tokens, expected)
            self.assertEqual(caught.exception.to_info().usage.model_calls, 1)
        diagnoser, _, _, task, execution, feedback, ctx = self.fixture()
        with patch("plugins.diagnosers.llm.monotonic", side_effect=[0, ctx.model_timeout_seconds + 1]):
            with self.assertRaises(DiagnosisError) as caught:
                diagnoser.diagnose(task, execution, feedback, ctx)
        self.assertEqual(caught.exception.to_info().usage.total_tokens, 5)

    def configure(self, root, rounds=2, mode="fixed"):
        path, _ = configured(root, rounds=rounds, mode=mode)
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        data["assessment"]["diagnoser"] = "llm_diagnoser"
        path.write_text(yaml.safe_dump(data), encoding="utf-8")
        return path

    def test_controller_charges_diagnosis_once_and_passes_analysis_to_evolver(self):
        with tempfile.TemporaryDirectory() as directory:
            startup = prepare_orchestrator(self.configure(Path(directory)), run_id="model-diagnosis")
            task = startup.prepare_task("dev-001")
            model, costs = attach_model(task, empty=True)
            original = model.make_response
            def respond(req, ctx):
                if json.loads(req.messages[-1].content).get("operation") == "failure_diagnosis":
                    value = response(analysis_text())
                    costs.append(value.usage)
                    return value
                return original(req, ctx)
            model.make_response = respond
            task.controller.diagnoser.model = model
            result = task.run()
            payloads = [json.loads(req.messages[-1].content) for req, _ in model.requests]
            self.assertEqual(sum(p.get("operation") == "failure_diagnosis" for p in payloads), 1)
            proposals = [p for p in payloads if p.get("operation") == "prompt_evolution"]
            self.assertEqual(len(proposals), 2)
            self.assertEqual(proposals[0]["context"]["diagnosis"]["explanation"], json.loads(analysis_text())["explanation"])
            self.assertEqual(len(proposals[0]["context"]["diagnosis"]["hypotheses"]), 1)
            self.assertIsNone(proposals[0]["context"]["diagnosis"]["record_ref"])
            self.assertEqual(result.usage.model_calls, 4)
            self.assertEqual(result.usage.total_tokens, sum(value.input_tokens + value.output_tokens for value in costs))
            text = (startup.execution.storage.run_root / "evolution.txt").read_text(encoding="utf-8")
            self.assertIn("诊断：tokens=5；模型调用=1", text)
            self.assertRegex(text, r"诊断_\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}_\d{6} → ")
            self.assertNotIn("报告异常", text)

    def test_model_diagnosis_failure_stops_before_routing_and_reports_cost(self):
        with tempfile.TemporaryDirectory() as directory:
            startup = prepare_orchestrator(self.configure(Path(directory)), run_id="diagnosis-error")
            task = startup.prepare_task("dev-001")
            attach_model(task)
            task.controller.diagnoser.model = RecordingModel(lambda *_: response("invalid JSON"))
            result = task.run()
            self.assertEqual(result.stop_reason, "diagnosis_error")
            self.assertEqual(result.evolution_rounds_used, 0)
            self.assertEqual(result.usage.model_calls, 2)
            self.assertEqual(result.usage.total_tokens, 10)
            text = (startup.execution.storage.run_root / "evolution.txt").read_text(encoding="utf-8")
            self.assertIn("原始诊断记录：诊断_", text)
            self.assertIn("诊断：tokens=5；模型调用=1", text)

    def test_standalone_diagnosis_filters_private_feedback_and_sums_its_usage(self):
        with tempfile.TemporaryDirectory() as directory:
            prepared = prepare_execution(self.configure(Path(directory), mode="disabled"), run_id="standalone")
            model = RecordingModel(lambda req, ctx: response(analysis_text()) if ctx.stage == "diagnosis" else response('{"answer":-1}'))
            prepared.agent.model = model
            prepared.diagnoser.model = model
            original = prepared.benchmark.evaluator.evaluate
            canary = "PRIVATE_SCORER_FEEDBACK_abc92"
            with patch.object(prepared.benchmark.evaluator, "evaluate", side_effect=lambda *args: original(*args).replace(feedback=canary)):
                record = prepared.run_task("dev-001")
            self.assertEqual(record.assessment_usage.total_tokens, 5)
            self.assertEqual(prepared.save_record(record)["usage"]["total_tokens"], 10)
            diagnosis_request = model.requests[-1][0].messages[-1].content
            self.assertNotIn(canary, diagnosis_request)
            self.assertNotIn('"request":', diagnosis_request)
            self.assertEqual(record.diagnosis.source, "model")


if __name__ == "__main__":
    unittest.main()
