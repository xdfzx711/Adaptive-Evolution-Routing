"""Native scorer semantics, public evidence, and both diagnosis/evolution paths."""

import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from aer_core.diagnostic_evidence import diagnosis_payload, evolution_context_payload, resolve_pointer
from aer_core.errors import ContractError, DiagnosisError
from aer_core.feedback import PublicFeedbackBuilder, validate_diagnosis
from aer_core.schemas.execution import Diagnosis, FeedbackView
from aer_core.schemas.feedback import ContextPolicy
from aer_core.scoring_feedback import scorer_feedback_policy, scoring_findings
from plugins.diagnosers.lightweight import LightweightDiagnoser
from plugins.evaluators.hotpotqa import HotpotQAEvaluator, HotpotQAEvaluatorSettings, METRICS, score_prediction
from plugins.evaluators.static_patch import StaticPatchEvaluator, StaticPatchSettings
from tests import domain_helpers
from tests.execution import test_llm_diagnoser as fixtures
from tests.execution.helpers import response


def hotpot_feedback(output, answer, facts):
    evaluator = HotpotQAEvaluator(HotpotQAEvaluatorSettings())
    metrics, _ = score_prediction(output, answer, facts)
    result = domain_helpers.evaluation(score=metrics['joint_f1'], success=metrics['joint_em'] == 1).replace(
        metrics=metrics, reason_code='incorrect_answer', assessment_version=evaluator.scorer_version,
        feedback='Official HotpotQA evaluation: incorrect_answer')
    policy = ContextPolicy(output_fields=['answer', 'supporting_facts'], **scorer_feedback_policy(evaluator))
    return PublicFeedbackBuilder(policy).feedback(result), result


def analysis(**changes):
    data = {'explanation': 'The public evaluation failed; its cause needs verification.',
        'observations': [{'text': 'The evaluator reported failure.',
                          'evidence_refs': [{'source': 'evaluation', 'path': '/success'}]}],
        'hypotheses': [{'text': 'The answer may need checking against the task.',
                        'evidence_refs': [{'source': 'execution', 'path': '/output/answer'}],
                        'verification': 'Recompute the answer from the public task and compare.'}],
        'unknowns': ['No hidden reasoning or reference answer was provided.']}
    data.update(changes)
    return json.dumps(data)


class ScoringFeedbackTests(unittest.TestCase):
    def test_legacy_records_and_optional_provider_remain_supported(self):
        legacy = FeedbackView.model_validate_json(json.dumps(dict(score=0, success=False,
            reason_code='incorrect_answer', public_feedback='Failed')))
        self.assertEqual(legacy.feedback_version, 'aer.feedback.v1')
        self.assertEqual(dict(legacy.metrics), {})
        self.assertIsNone(legacy.scoring_rules)
        self.assertEqual(Diagnosis(reason_code='unknown_failure', explanation='Unknown').unknowns, ())
        evaluator = SimpleNamespace(scorer_version='custom.v1', score_policy='Custom public policy')
        policy = scorer_feedback_policy(evaluator, 'custom')
        self.assertEqual(policy['scoring_rules'].completeness, 'summary_only')
        self.assertEqual(policy['scoring_rules'].rules, {})
        result = domain_helpers.evaluation().replace(feedback='PRIVATE_REFERENCE_CANARY')
        feedback = PublicFeedbackBuilder(ContextPolicy(output_fields=['answer'], **policy)).feedback(result)
        self.assertNotIn('PRIVATE_REFERENCE_CANARY', feedback.model_dump_json())
        self.assertEqual(dict(feedback.metrics), {})
        evaluator.public_metric_fields = ('score',)
        declared = PublicFeedbackBuilder(ContextPolicy(output_fields=['answer'], **scorer_feedback_policy(evaluator))).feedback(result)
        self.assertEqual(dict(declared.metrics), dict(result.metrics))

    def test_all_native_metrics_and_provenance_reach_both_paths_without_private_labels(self):
        feedback, result = hotpot_feedback({'answer': 'wrong', 'supporting_facts': []},
                                          'PRIVATE_REFERENCE_CANARY', [['HiddenTitle', 0]])
        self.assertEqual(set(feedback.metrics), set(METRICS))
        self.assertEqual(dict(feedback.metrics), dict(result.metrics))
        self.assertEqual(feedback.evaluation_id, result.evaluation_id)
        self.assertEqual(feedback.execution_operation_id, result.execution_operation_id)
        self.assertEqual(feedback.evaluator_feedback, result.feedback)
        self.assertEqual(feedback.evaluator_reason_code, result.reason_code)
        self.assertNotIn('PRIVATE_REFERENCE_CANARY', feedback.model_dump_json())
        self.assertNotIn('HiddenTitle', feedback.model_dump_json())
        diagnoser, model, _, task, execution, _, ctx = fixtures.LLMDiagnoserTests().fixture(lambda *_: response(analysis()))
        diagnosis = diagnoser.diagnose(task, execution, feedback, ctx)
        diagnostic_input = json.loads(model.requests[0][0].messages[-1].content)
        evolution = domain_helpers.context().replace(task=task, execution=execution,
            stable_snapshot=domain_helpers.snapshot('v0'), feedback=feedback, diagnosis=diagnosis)
        payload = evolution_context_payload(evolution)
        self.assertEqual(payload['feedback'], diagnostic_input['feedback'])
        self.assertEqual(payload['diagnosis']['unknowns'], list(diagnosis.unknowns))
        for name in ('record_ref', 'usage', 'confidence', 'fact_check', 'evidence_key'):
            self.assertNotIn(name, payload['diagnosis'])
        self.assertIsNotNone(diagnosis.record_ref)

    def test_three_observed_failure_patterns_follow_native_semantics(self):
        # Excess words, not terminal punctuation, reduce answer precision.
        outputs = [{'answer': 'red fox animal.', 'supporting_facts': [['Doc', 0]]},
                   {'answer': 'red fox animal', 'supporting_facts': [['Doc', 0]]}]
        a, _ = hotpot_feedback(outputs[0], 'red fox', [['Doc', 0]])
        b, _ = hotpot_feedback(outputs[1], 'red fox', [['Doc', 0]])
        self.assertEqual(a.metrics, b.metrics)
        self.assertIn('answer_excess_tokens', {x.code for x in a.findings})
        self.assertIn('ASCII punctuation', a.scoring_rules.rules['answer_normalization'])
        # A verbose yes fails answer scoring despite fully correct evidence.
        yes, _ = hotpot_feedback({'answer': 'Yes, both worked in the music industry.',
            'supporting_facts': [['Doc', 0]]}, 'yes', [['Doc', 0]])
        self.assertEqual(yes.metrics['f1'], 0)
        self.assertEqual(yes.metrics['sp_em'], 1)
        self.assertIn('supporting_facts_exact', {x.code for x in yes.findings})
        # Two correct facts out of three must not be described as wrong pairs.
        missing, _ = hotpot_feedback({'answer': 'Group Health', 'supporting_facts': [['Doc', 0], ['Doc', 1]]},
            'Group Health', [['Doc', 0], ['Doc', 1], ['Doc', 2]])
        self.assertEqual(missing.metrics['joint_f1'], 0.8)
        self.assertEqual({x.code for x in missing.findings}, {'answer_exact', 'supporting_facts_incomplete'})
        self.assertEqual(scoring_findings({}, missing.scoring_rules), ())
        self.assertEqual(scoring_findings({'sp_recall': 0.5}, missing.scoring_rules), ())

    def test_other_benchmark_proxy_does_not_imply_test_success(self):
        evaluator = StaticPatchEvaluator(StaticPatchSettings())
        policy = ContextPolicy(output_fields=['patch'], **scorer_feedback_policy(evaluator))
        feedback = PublicFeedbackBuilder(policy).feedback(domain_helpers.evaluation().replace(metrics={'tests_executed': 0}))
        self.assertEqual(feedback.findings[0].code, 'tests_not_executed')
        self.assertIn('static similarity proxy', feedback.scoring_rules.rules['limits'])

    def test_rules_must_match_evaluator_and_evaluation_versions(self):
        evaluator = HotpotQAEvaluator(HotpotQAEvaluatorSettings())
        policy = ContextPolicy(output_fields=['answer'], **scorer_feedback_policy(evaluator))
        with self.assertRaises(ContractError):
            PublicFeedbackBuilder(policy).feedback(domain_helpers.evaluation().replace(assessment_version='other.v1'))
        with patch.object(evaluator, 'public_scoring_rules', return_value=evaluator.public_scoring_rules().replace(scorer_version='other')):
            with self.assertRaises(ContractError):
                scorer_feedback_policy(evaluator)

    def test_standalone_alternate_scorer_uses_unknown_rules_not_configured_rules(self):
        evaluator = HotpotQAEvaluator(HotpotQAEvaluatorSettings())
        policy = scorer_feedback_policy(evaluator, assessment_version='external.v1')
        feedback = PublicFeedbackBuilder(ContextPolicy(output_fields=['answer'], **policy)).feedback(
            domain_helpers.evaluation().replace(assessment_version='external.v1', metrics={'private_key': 1.0}))
        self.assertEqual(feedback.scoring_rules.scorer_version, 'external.v1')
        self.assertEqual(feedback.scoring_rules.completeness, 'summary_only')
        self.assertEqual(dict(feedback.metrics), {})
        self.assertIsNone(feedback.evaluator_id)

    def test_rule_diagnosis_preserves_findings_and_historical_refs_are_not_rebound(self):
        feedback, _ = hotpot_feedback({'answer': 'yes', 'supporting_facts': [['Doc', 0]]}, 'yes', [['Doc', 0], ['Doc', 1]])
        _, _, _, task, execution, _, ctx = fixtures.LLMDiagnoserTests().fixture()
        diagnosis = LightweightDiagnoser().diagnose(task, execution, feedback, ctx)
        builder = PublicFeedbackBuilder(ContextPolicy(output_fields=['answer']))
        current = builder.diagnosis_view(diagnosis, [1], feedback=feedback)
        self.assertEqual(current.observations, diagnosis.observations)
        self.assertTrue(current.unknowns)
        validate_diagnosis(current, execution, feedback, task)
        historical = builder.diagnosis_view(diagnosis, (), historical=True)
        self.assertTrue(all(not item.evidence_refs for item in historical.observations))

    def test_new_schema_uses_evaluator_category_and_explicit_unknowns(self):
        diagnoser, model, _, task, execution, feedback, ctx = fixtures.LLMDiagnoserTests().fixture(lambda *_: response(analysis()))
        result = diagnoser.diagnose(task, execution, feedback, ctx)
        self.assertEqual(result.reason_code, feedback.reason_code)
        self.assertEqual(result.diagnosis_version, 'aer.diagnosis.v2')
        self.assertIsNone(result.confidence)
        payload = json.loads(model.requests[0][0].messages[-1].content)
        self.assertEqual(set(payload['diagnosis_schema']['properties']), {'explanation', 'observations', 'hypotheses', 'unknowns'})
        self.assertNotIn('allowed_reason_codes', payload)

    def test_invalid_public_references_and_missing_verification_fail_with_usage(self):
        for source, pointer in [('evaluation', '/private_parameters/answer'), ('execution', '/trajectory/1'),
                                ('scoring_rules', '/rules/answer'), ('execution', '/trajectory/01'), ('task', '/input/~3')]:
            with self.subTest(source=source, pointer=pointer):
                content = analysis(observations=[{'text': 'Unsupported', 'evidence_refs': [{'source': source, 'path': pointer}]}])
                diagnoser, _, _, task, execution, feedback, ctx = fixtures.LLMDiagnoserTests().fixture(lambda *_: response(content))
                with self.assertRaises(DiagnosisError) as caught:
                    diagnoser.diagnose(task, execution, feedback, ctx)
                self.assertEqual(caught.exception.to_info().usage.total_tokens, 5)
        for changes in [dict(observations=[dict(text='No evidence', evidence_refs=[])]),
                        dict(hypotheses=[dict(text='Guess', evidence_refs=[], verification=' ')])]:
            diagnoser, _, _, task, execution, feedback, ctx = fixtures.LLMDiagnoserTests().fixture(lambda *_: response(analysis(**changes)))
            with self.assertRaises(DiagnosisError):
                diagnoser.diagnose(task, execution, feedback, ctx)
        self.assertEqual(resolve_pointer({'a/b': {'~': [False]}}, '/a~1b/~0/0'), False)

    def test_cache_key_changes_with_metrics_rules_or_prompt(self):
        diagnoser, _, _, task, execution, feedback, _ = fixtures.LLMDiagnoserTests().fixture()
        key = diagnoser.evidence_key(task, execution, feedback)
        self.assertEqual(key, diagnoser.evidence_key(task, execution, feedback))
        self.assertNotEqual(key, diagnoser.evidence_key(task, execution, feedback.replace(metrics={'f1': 0.3})))
        rules = HotpotQAEvaluator(HotpotQAEvaluatorSettings()).public_scoring_rules()
        self.assertNotEqual(key, diagnoser.evidence_key(task, execution, feedback.replace(scoring_rules=rules)))
        with patch('plugins.diagnosers.llm.SYSTEM_PROMPT', 'New diagnosis rules'):
            self.assertNotEqual(key, diagnoser.evidence_key(task, execution, feedback))
