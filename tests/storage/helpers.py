from aer_core.schemas.evolution import EvolutionCandidate
from aer_core.schemas.experience import CandidateEvaluation, CommitRequest, EvolutionAttempt
from aer_core.schemas.references import AgentSnapshot
from aer_core.schemas.validation import ValidationReport
from plugins.storage.artifacts import LocalArtifactStore
from plugins.storage.experience import JsonlExperienceStore
from plugins.storage.versions import LocalVersionStore
from tests.domain_helpers import NOW, acceptance, artifact, metrics, remaining
from aer_core.schemas.evolution import EvolutionDecision
from aer_core.schemas.usage import ResourceUsage


def stores(root, scope="default"):
    artifacts = LocalArtifactStore(root)
    experience = JsonlExperienceStore(root)
    versions = LocalVersionStore(root, artifacts, experience, scope_id=scope)
    return artifacts, versions, experience


def initial(artifacts, versions):
    prompt = artifacts.put(b"original prompt", "prompt")
    snapshot = AgentSnapshot(version_id="v1", agent_plugin_id="mock_agent_a", config={"nested": {"steps": [1, 2]}}, artifacts={"prompt": prompt})
    versions.initialize(snapshot)
    return snapshot


def prepare_candidate(artifacts, versions, stable, name="candidate-1", version="v2"):
    changed = artifacts.put(f"updated prompt {name}".encode(), "prompt")
    candidate = EvolutionCandidate(candidate_id=name, parent_version_id=stable.version_id, target="prompt", artifact_updates={"prompt": changed}, candidate_version_id=version)
    return candidate, versions.materialize(stable, candidate)


def validation(artifacts, versions, stable, snapshot, candidate_id="candidate-1", validation_id="validation-1"):
    evidence = artifacts.put(b'{"mock_validation":true}', "validation_results")
    local = artifacts.put(b'{"mock_local":true}', "local_results")
    usage = ResourceUsage(input_tokens=1, output_tokens=1, total_tokens=2, model_calls=1, tool_calls=0, elapsed_seconds=1, usage_complete=True, usage_sources=["simulated"])
    report = ValidationReport(validation_id=validation_id, candidate_id=candidate_id, baseline_version_id=stable.version_id, candidate_version_id=snapshot.version_id, status="complete", baseline_metrics=metrics(), candidate_metrics=metrics(True), local_result_ref=local, private_results_ref=evidence, candidate_validation_tokens=2, usage=usage)
    return report, versions.save_validation(report)


def commit_request(artifacts, versions, stable, candidate_id="candidate-1", version="v2", attempt_id="attempt-1"):
    candidate, snapshot = prepare_candidate(artifacts, versions, stable, candidate_id, version)
    report, ref = validation(artifacts, versions, stable, snapshot, candidate_id, "validation-" + candidate_id)
    accepted = acceptance().replace(candidate_id=candidate_id)
    request = CommitRequest(run_id="run-1", episode_id="episode-1", attempt_id=attempt_id, candidate_id=candidate_id, expected_stable_version_id=stable.version_id, validated_snapshot=snapshot, validation_ref=ref, acceptance=accepted)
    return request, report


def attempt(status="completed", candidate_ids=("candidate-1",), attempt_id="attempt-1"):
    return EvolutionAttempt(attempt_id=attempt_id, run_id="run-1", episode_id="episode-1", task_id="dev-001", round_id=1, stable_version_id="v1", pre_state_ref=artifact("state"), trajectory_ref=artifact("trace"), feasible_targets=["prompt"], decision=EvolutionDecision(target="prompt", rationale="fixed"), operator_id="mock_prompt_evolver_a", candidate_ids=candidate_ids, status=status, error=None, budget_before=remaining(), budget_after=remaining(), usage=ResourceUsage.zero(), started_at=NOW, finished_at=NOW)


def outcome(ref, *, accepted=True, committed=False):
    result = acceptance(accepted)
    return CandidateEvaluation(attempt_id="attempt-1", candidate_id="candidate-1", candidate_version_id="v2", validation_ref=ref, acceptance=result, status="committed" if committed else "accepted" if accepted else "rejected", selected=committed, committed=committed, commit_error=None, final_recorded_at=NOW)
