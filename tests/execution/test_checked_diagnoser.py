import json
import unittest

from aer_core.errors import DiagnosisError, OperationTimeout
from aer_core.feedback import PublicFeedbackBuilder, validate_diagnosis
from aer_core.schemas.diagnosis import DiagnosisRecord
from aer_core.schemas.execution import CheckDiagnosis, PublicCheckResult
from aer_core.schemas.feedback import ContextPolicy
from tests.execution.helpers import response
from tests.execution import test_llm_diagnoser as legacy
from tests.domain_helpers import evaluation


CHECKS = (PublicCheckResult(check_id="overlap", status="failed"),
          PublicCheckResult(check_id="keyword", status="passed"))


def checked_text(status="passed", hypotheses=None):
    return json.dumps({"checks": [
        {"check_id": "overlap", "status": "failed", "hypotheses": hypotheses or []},
        {"check_id": "keyword", "status": status, "hypotheses": []}], "confidence": None})


class CheckedDiagnosisTests(unittest.TestCase):
    def fixture(self, respond=None):
        d, m, store, task, execution, feedback, ctx = legacy.LLMDiagnoserTests().fixture(
            respond or (lambda *_: response(checked_text())))
        execution = execution.replace(output={"response": "The keyword giraffe adds no relevant criterion to this comparison."})
        return d, m, store, task, execution, feedback.replace(checks=CHECKS), ctx

    def test_authoritative_facts_and_hypotheses_are_separate(self):
        hypothesis = {"text": "Possible mismatch in wording overlap", "evidence_step_ids": [1], "check_ids": ["overlap"]}
        d, m, store, t, e, f, c = self.fixture(lambda *_: response(checked_text(hypotheses=[hypothesis])))
        got = validate_diagnosis(d.diagnose(t, e, f, c), e, f)
        self.assertEqual(got.consistency_status, "verified")
        self.assertIn("keyword: passed", got.explanation)
        self.assertEqual(got.hypotheses[0].check_ids, ("overlap",))
        self.assertEqual(got.usage.total_tokens, 5)
        receipt = DiagnosisRecord.model_validate_json(store.get(got.record_ref))
        self.assertEqual(len(receipt.attempts), 1)
        payload = json.loads(m.requests[0][0].messages[-1].content)
        self.assertEqual(payload['feedback']['checks'][1]['status'], 'passed')
        self.assertEqual(got.evidence_key, d.evidence_key(t, e, f))

    def test_actual_old_misdiagnosis_shape_is_corrected_once(self):
        old = legacy.analysis_text(explanation="第7句未包含关键词giraffe",
            observations=[{"text": "第7句为 The keyword giraffe adds no relevant criterion to this comparison.",
                           "evidence_step_ids": [1]}])
        values = iter([response(old), response(checked_text())])
        d, m, store, t, e, f, c = self.fixture(lambda *_: next(values))
        got = d.diagnose(t, e, f, c)
        self.assertEqual(got.consistency_status, "corrected")
        self.assertNotIn("未包含", got.explanation)
        self.assertEqual(got.usage.model_calls, 2)
        self.assertEqual(got.usage.total_tokens, 10)
        receipt = DiagnosisRecord.model_validate_json(store.get(got.record_ref))
        self.assertTrue(receipt.attempts[0].conflicts)
        self.assertFalse(receipt.attempts[1].conflicts)
        self.assertEqual(json.loads(m.requests[1][0].messages[-1].content)['operation'], 'correct_diagnosis')

    def test_conflicting_verdicts_fall_back_without_changing_scores(self):
        d, m, store, t, e, f, c = self.fixture(lambda *_: response(checked_text(status="failed")))
        got = d.diagnose(t, e, f, c)
        self.assertEqual(got.consistency_status, "fallback")
        self.assertEqual(got.source, "rule")
        self.assertFalse(got.hypotheses)
        self.assertEqual(got.checks[1].status, "passed")
        self.assertEqual(len(m.requests), 2)
        self.assertEqual(f.score, 0)
        receipt = DiagnosisRecord.model_validate_json(store.get(got.record_ref))
        self.assertIsNone(receipt.error)
        self.assertEqual(receipt.usage.total_tokens, 10)

    def test_passed_unknown_missing_duplicate_and_invented_checks_are_not_accepted(self):
        good = json.loads(checked_text())
        cases = [[], good['checks'] + [good['checks'][0]],
                 [good['checks'][0], {'check_id': 'invented', 'status': 'passed', 'hypotheses': []}],
                 [good['checks'][0], {'check_id': 'keyword', 'status': 'passed',
                                     'hypotheses': [{'text': 'wrong failure'}]}]]
        for checks in cases:
            with self.subTest(checks=checks):
                d, _, _, t, e, f, c = self.fixture(lambda *_: response(json.dumps({'checks': checks})))
                self.assertEqual(d.diagnose(t, e, f, c).consistency_status, 'fallback')
        d, _, _, t, e, f, c = self.fixture()
        f = f.replace(checks=[CHECKS[0], CHECKS[1].replace(status='unknown')])
        self.assertEqual(d.diagnose(t, e, f, c).checks[1].status, 'unknown')

    def test_evidence_or_check_reference_errors_are_corrected(self):
        for hypo in ({'text': 'cause', 'evidence_step_ids': [99]},
                     {'text': 'cause', 'check_ids': ['keyword']}):
            values = iter([response(checked_text(hypotheses=[hypo])), response(checked_text())])
            d, _, _, t, e, f, c = self.fixture(lambda *_: next(values))
            self.assertEqual(d.diagnose(t, e, f, c).consistency_status, 'corrected')

    def test_provider_error_remains_error_not_causal_fallback(self):
        def fail(*_): raise OperationTimeout('provider timeout', stage='diagnosis')
        d, m, store, t, e, f, c = self.fixture(fail)
        with self.assertRaises(DiagnosisError) as caught: d.diagnose(t, e, f, c)
        receipt = DiagnosisRecord.model_validate_json(store.get(caught.exception.to_info().evidence_ref))
        self.assertIsNone(receipt.diagnosis)
        self.assertEqual(len(m.requests), 1)

    def test_reuse_key_changes_for_execution_scoring_and_check_verdict(self):
        d, _, _, t, e, f, _ = self.fixture()
        key = d.evidence_key(t, e, f)
        self.assertNotEqual(key, d.evidence_key(t, e.replace(agent_version_id='new'), f))
        self.assertNotEqual(key, d.evidence_key(t, e.replace(output={'response': 'changed'}), f))
        self.assertNotEqual(key, d.evidence_key(t, e, f.replace(assessment_version='new-scorer')))
        self.assertNotEqual(key, d.evidence_key(t, e, f.replace(checks=[CHECKS[0].replace(status='passed'), CHECKS[1]])))

    def test_default_denies_checks_and_allowlist_does_not_forward_private_feedback(self):
        ev = evaluation().replace(public_checks=[*CHECKS, PublicCheckResult(check_id='private', status='failed',
            explanation='PRIVATE_EXPECTED_ANSWER')], feedback='PRIVATE_RAW_SCORER_PROSE')
        self.assertFalse(PublicFeedbackBuilder(ContextPolicy(output_fields=[])).feedback(ev).checks)
        builder = PublicFeedbackBuilder(ContextPolicy(output_fields=[], public_check_ids=['overlap','keyword']))
        public = builder.feedback(ev)
        self.assertEqual(public.checks, CHECKS)
        self.assertNotIn('PRIVATE', public.model_dump_json())
        d, _, _, t, e, f, c = self.fixture()
        got = d.diagnose(t,e,f,c)
        self.assertEqual(builder.diagnosis_view(got, [1]).checks, got.checks)


if __name__ == '__main__': unittest.main()
