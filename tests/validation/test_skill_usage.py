from pathlib import Path
import tempfile
import unittest

from aer_core.schemas.evolution import EvolutionCandidate
from aer_core.schemas.paired import ValidationEvidence
from aer_core.schemas.references import AgentSnapshot
from aer_core.schemas.skills import SkillLibrary
from aer_core.schemas.validation import ValidationPlan, PairSeed
from plugins.skills.library import decode_library, encode_library, parse_skill
from plugins.skills.runtime import SkillRuntime
from plugins.storage.artifacts import LocalArtifactStore
from plugins.storage.versions import LocalVersionStore
from plugins.validation.acceptance import ImprovementWithRegressionGuard
from plugins.validation.cache import LocalValidationCache
from plugins.validation.paired import PairedValidation
from tests.domain_helpers import task
from tests.execution.helpers import context
from tests.validation.helpers import ScoreExecutor, settings


def document(body="OLD BODY", description="Original trigger."):
    return parse_skill(f"---\nname: guide\ndescription: {description}\n---\n{body}")


class SkillExecutor(ScoreExecutor):
    def __init__(self, artifacts, *, baseline_read=False, candidate_read=True, tamper=None, missing=False):
        super().__init__()
        self.artifacts = artifacts
        self.baseline_read, self.candidate_read = baseline_read, candidate_read
        self.tamper, self.missing = tamper, missing

    def execute(self, task, snapshot, ctx):
        scored = super().execute(task, snapshot, ctx)
        runtime = SkillRuntime.from_snapshot(snapshot, self.artifacts, ctx.operation_id)
        if self.candidate_read if snapshot.parent_version_id else self.baseline_read:
            runtime.read_skill("guide", call_id="r1")
        state = runtime.summary("mcp", discovered=True)
        if snapshot.parent_version_id and self.tamper:
            key, value = self.tamper
            if key == "observed_library":
                state["skill_library_artifact_id"] = value
            else:
                state["skill_reads"][0][key] = value
        if snapshot.parent_version_id and self.missing:
            state = {"read_skills": ["guide"]}
        return scored.replace(execution=scored.execution.replace(state_summary=state))


def setup(root, *, body="NEW BODY", description="Original trigger.", **options):
    store = LocalArtifactStore(root)
    versions = LocalVersionStore(root, store)
    before = SkillLibrary(skills=[document()])
    after = SkillLibrary(skills=[document(body, description)])
    stable = AgentSnapshot(version_id="stable", agent_plugin_id="fixture_agent", config={}, artifacts={
        "prompt": store.put(b"task", "prompt"), "skill": store.put(encode_library(before), "skill")})
    versions.initialize(stable)
    candidate = EvolutionCandidate(candidate_id="candidate", parent_version_id="stable", target="skill",
        artifact_updates={"skill": store.put(encode_library(after), "skill")})
    snapshot = versions.materialize(stable, candidate)
    candidate = candidate.replace(candidate_version_id=snapshot.version_id)
    similar, regression = task("similar1"), task("reg1")
    plan = ValidationPlan(current_task=task("local"), baseline_execution=None, baseline_evaluation=None,
        similar_tasks=[similar], regression_tasks=[regression],
        pair_seeds=[PairSeed(task=item.identity, seed=7) for item in (similar, regression)],
        policy_id="improvement_with_regression_guard", policy_version="v0.1")
    runner = PairedValidation(SkillExecutor(store, **options), store, versions, LocalValidationCache(root), {})
    return store, versions, stable, candidate, plan, runner


class PairedSkillUsageTests(unittest.TestCase):
    def test_discovery_is_per_skill_and_malformed_receipts_are_not_evidence(self):
        from aer_core.skill_usage import observe_skill
        with tempfile.TemporaryDirectory() as directory:
            store, versions, _, candidate, _, runner = setup(Path(directory))
            snapshot = versions.load(candidate.candidate_version_id)
            library = decode_library(store.get(snapshot.artifacts["skill"]))
            execution = runner.executor.execute(task("local"), snapshot, context()).execution
            for fields, expected in (({"available_skills": []}, False), ({"skill_discovered": False}, False),
                                     ({"skill_discovered": None}, None)):
                state = {**execution.model_dump(mode="json")["state_summary"], **fields}
                evidence = observe_skill(execution.replace(state_summary=state), snapshot, library, "guide")
                self.assertEqual(evidence["discovered"], expected)
            for receipts in (None, [None], [{"name": "guide", "delivery": "read", "status": "completed", "call_id": []}]):
                state = {**execution.model_dump(mode="json")["state_summary"], "skill_reads": receipts}
                evidence = observe_skill(execution.replace(state_summary=state), snapshot, library, "guide")
                self.assertFalse(evidence["read_verified"])
                self.assertTrue(evidence["issues"])

    def run_case(self, parts, operation="validation"):
        store, _, stable, candidate, plan, runner = parts
        report = runner.validate(stable, [candidate], plan, context(stage="local_validation", operation_id=operation))[0]
        local = ValidationEvidence.model_validate_json(store.get(report.local_result_ref))
        return report, local.results[0]

    def test_score_gain_without_read_is_not_body_improvement_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            parts = setup(Path(directory), candidate_read=False)
            report, pair = self.run_case(parts)
            self.assertEqual(report.status, "complete")
            self.assertTrue(ImprovementWithRegressionGuard(settings()).evaluate(report).accepted)
            usage = pair.skill_usage["skills"]["guide"]
            self.assertTrue(usage["candidate"]["available"])
            self.assertTrue(usage["candidate"]["discovered"])
            self.assertFalse(usage["candidate"]["read_verified"])
            self.assertGreater(pair.skill_usage["score_delta"], 0)
            self.assertEqual(usage["body_attribution"], "not_read_cannot_attribute")
            self.assertEqual(parts[1].stable(), parts[2])

    def test_matching_digest_proves_exposure_only_and_cached_receipts_keep_execution_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            parts = setup(Path(directory))
            _, first = self.run_case(parts)
            _, second = self.run_case(parts, "another-validation")
            self.assertTrue(second.candidate_cached)
            self.assertTrue(second.skill_usage["candidate_cached"])
            usage = second.skill_usage["skills"]["guide"]
            self.assertTrue(usage["candidate"]["read_verified"])
            self.assertEqual(usage["candidate"]["verified_read_count"], 1)
            self.assertEqual(usage["candidate"]["reads"][0]["execution_operation_id"], first.candidate.execution.operation_id)
            self.assertEqual(usage["body_attribution"], "verified_exposure_only_not_causal")

    def test_description_only_change_is_analyzed_as_selection_not_body_gain(self):
        with tempfile.TemporaryDirectory() as directory:
            _, pair = self.run_case(setup(Path(directory), body="OLD BODY", description="Better trigger."))
            usage = pair.skill_usage["skills"]["guide"]
            self.assertFalse(usage["change"]["body_changed"])
            self.assertTrue(usage["change"]["description_changed"])
            self.assertTrue(usage["selection_changed"])
            self.assertEqual(usage["body_attribution"], "not_applicable")
            self.assertEqual(usage["description_attribution"], "selection_comparison_only_not_causal")

    def test_wrong_snapshot_library_digest_or_execution_blocks_acceptance(self):
        for key in ("snapshot_version_id", "skill_library_artifact_id", "content_digest", "execution_operation_id", "observed_library"):
            with self.subTest(key=key), tempfile.TemporaryDirectory() as directory:
                report, pair = self.run_case(setup(Path(directory), tamper=(key, "wrong")))
                self.assertEqual(report.status, "error")
                self.assertFalse(pair.skill_usage["integrity_ok"])
                self.assertFalse(pair.candidate.complete)
                self.assertFalse(ImprovementWithRegressionGuard(settings()).evaluate(report).accepted)

    def test_missing_legacy_receipts_are_unknown_not_verified_or_known_unread(self):
        with tempfile.TemporaryDirectory() as directory:
            _, pair = self.run_case(setup(Path(directory), missing=True))
            usage = pair.skill_usage["skills"]["guide"]
            self.assertIsNone(usage["candidate"]["discovered"])
            self.assertIsNone(usage["selection_changed"])
            self.assertEqual(usage["body_attribution"], "unverified_read_cannot_attribute")
            self.assertIn("declared_read_without_receipt", usage["candidate"]["issues"])
