"""Native SkillClaw pipeline; AER owns evidence, candidates and promotion."""

import asyncio
import json
from pathlib import Path

from aer_core.errors import AERError, BudgetExceeded, EvolutionError
from aer_core.execution_evidence import execution_evidence
from aer_core.schemas.construction import ConstructionFeedback, ConstructionIssue
from aer_core.schemas.evolution import EvolutionCandidate, EvolutionCapabilities, EvolutionResponse
from plugins.agents.execution import SessionRecord
from plugins.skills.library import decode_library, encode_library
from .exporter import apply_proposal
from .model_adapter import ModelAdapter
from .sessions import export_session
from .source import UPSTREAM_REVISION, load_upstream


class SkillClawSkillEvolver:
    plugin_id = "skillclaw_skill_evolver"

    def __init__(self, config, artifacts, model, *, config_root=None):
        self.config, self.artifacts, self.model = config, artifacts, model
        source = Path(config.source_path).expanduser() if config.source_path else None
        self.source = (config_root / source).resolve() if source and not source.is_absolute() and config_root else source

    def capabilities(self):
        return EvolutionCapabilities(supported_targets=["skill"], compatible_agents=["codex_cli_agent", "react_agent"],
            required_artifact_kinds=["skill"], supports_multiple_candidates=False, reports_usage=True,
            target_descriptions={"skill": "Use the pinned SkillClaw workflow to improve or create reusable skills "
                "for domain knowledge and environment procedures supported by public execution evidence. "
                "Can update a read skill's body or description, create a distinct skill, or skip with a reason. "
                "Correct instructions ignored by the Agent are not a demonstrated skill defect. "
                "Unread skills do not establish a knowledge gap; the no-skill branch creates or skips and does not "
                "scan all unread skills to optimize descriptions. Preserves prompt, executable tools and model. "
                "AER checks candidate discovery/read/version evidence and paired scores before promotion."})

    def is_applicable(self, snapshot):
        # ReAct preload is deliberately not advertised as observed skill use.
        return snapshot.agent_plugin_id in self.capabilities().compatible_agents and "skill" in snapshot.artifacts

    async def _pipeline(self, upstream, adapter, session, original, events):
        adapter.stage = "summarize"
        await upstream.summarizer.summarize_sessions_parallel(adapter, [session])
        adapter.check()
        groups = upstream.aggregation.aggregate_sessions_by_skill([session])
        events.append({"kind": "groups", "groups": {name: [item["session_id"] for item in sessions]
                                                     for name, sessions in sorted(groups.items())}})
        library, changes = original, []
        for name, sessions in sorted(groups.items()):
            adapter.check()
            if len(changes) >= self.config.max_skill_changes:
                events.append({"kind": "group_deferred", "name": name, "reason": "max_skill_changes"})
                continue
            names = [skill.name for skill in library.skills]
            document = next((skill for skill in library.skills if skill.name == name), None)
            if name == "__no_skill__":
                events.append({"kind": "no_skill_boundary", "available_skills": names,
                    "unread_skills_inspected": False, "missing_capability_established": False,
                    "possible_explanations": ["skill_description_mismatch", "agent_did_not_select", "missing_skill", "insufficient_evidence"],
                    "actions": ["create_skill", "skip"]})
                adapter.stage = "create"
                current = None
                proposal = await upstream.execution.create_skill_from_sessions(adapter, sessions, names)
            else:
                if document is None:
                    raise ValueError("Referenced skill is missing from the frozen library")
                adapter.stage = "improve"
                # Normalize CRLF for upstream's minimal parser; original bytes stay in the AER library.
                current = upstream.utils.parse_skill_content(name, document.content.replace("\r\n", "\n"))
                proposal = await upstream.execution.evolve_skill_from_sessions(adapter, name, sessions, current, names)
            adapter.check()
            events.append({"kind": "proposal", "source_skill": name, "value": proposal})
            if isinstance(proposal, dict) and proposal.get("action") == "skip" and (
                    not isinstance(proposal.get("rationale"), str) or not proposal["rationale"].strip()):
                raise ValueError("SkillClaw skip requires an explicit rationale")
            updated, change = apply_proposal(library, proposal, source_name=name if document else None,
                max_changes=self.config.max_skill_changes, changed=[item["name"] for item in changes])
            if change is None:
                events.append({"kind": "unchanged", "name": name, "action": proposal.get("action"),
                    "reason": proposal.get("rationale", ""), "source": "model_assessment"})
                continue
            change.update(source_group=name, missing_capability_established=False)
            if self.config.verify_candidates:
                adapter.stage = "verify"
                selected = next(skill for skill in updated.skills if skill.name == change["name"])
                verification = await upstream.verifier.verify_skill_candidate(adapter,
                    upstream.utils.parse_skill_content(selected.name, selected.content.replace("\r\n", "\n")),
                    sessions, change["action"], current_skill=current)
                adapter.check()
                events.append({"kind": "verification", "name": selected.name, "value": verification})
                if not verification.get("accepted"):
                    continue
            library = updated
            changes.append(change)
        return library, changes

    def evolve(self, request):
        record, calls, events = SessionRecord(), [], []
        candidates, changes, issues = [], [], []
        evidence, session, adapter, error, status = None, None, None, None, "empty"
        try:
            if request.decision.target != "skill" or not self.is_applicable(request.context.stable_snapshot):
                raise ValueError("SkillClaw requires a compatible Agent with a frozen skill library")
            original = decode_library(self.artifacts.get(request.context.stable_snapshot.artifacts["skill"]))
            evidence = execution_evidence(request.context, original)
            if evidence.injected_skills:
                raise ValueError("SkillClaw v1 supports observed on-demand reads, not preloaded sessions")
            session = export_session(evidence, self.config)
            feedback = {"feedback": evidence.feedback.model_dump(mode="json"),
                        "execution_output": evidence.model_dump(mode="json")["output"],
                        "execution_usage": evidence.usage.model_dump(mode="json"),
                        "diagnosis": evidence.diagnosis.model_dump(mode="json") if evidence.diagnosis else None,
                        "history": [item.model_dump(mode="json") for item in request.context.history],
                        "provenance": session["aer_provenance"]}
            adapter = ModelAdapter(self.model, request.run_context, self.config, record, calls, feedback)
            upstream = load_upstream(self.source)
            library, changes = asyncio.run(self._pipeline(upstream, adapter, session, original, events))
            adapter.check()
            if library != original:
                ref = self.artifacts.put(encode_library(library), "skill")
                candidates = [EvolutionCandidate(candidate_id=request.attempt_id + "/candidate/1",
                    parent_version_id=request.context.stable_snapshot.version_id, target="skill", artifact_updates={"skill": ref},
                    metadata={"operator": self.plugin_id, "upstream_revision": UPSTREAM_REVISION,
                        "rationale": "; ".join(item["rationale"] for item in changes), "skill_changes": changes,
                        "change_summary": "; ".join(item["action"] + ": " + item["name"] for item in changes),
                        "activation": "requires_aer_validation_and_commit"})]
                status = "completed"
            else:
                issues.append(ConstructionIssue(stage="judgment", code="no_skill_change", source="model_assessment",
                    message="SkillClaw produced no eligible skill change; see proposal and verification receipts",
                    details={"decisions": [event for event in events if event["kind"] in {"unchanged", "verification"}]}))
        except Exception as failure:
            status = "budget_interrupted" if isinstance(failure, BudgetExceeded) else "error"
            error = failure.to_info() if isinstance(failure, AERError) else EvolutionError(
                "SkillClaw evolution failed: " + str(failure), stage="evolution", plugin_id=self.plugin_id).to_info()
            issues.append(ConstructionIssue(stage="termination", code=error.code, source="observation", message=error.message))
        usage = record.usage()
        if error:
            error = error.replace(stage="evolution", operation_id=request.run_context.operation_id, usage=usage)
        receipt = {"schema_version": "aer.skill_evolution.v1", "attempt_id": request.attempt_id,
            "parent_version_id": request.context.stable_snapshot.version_id, "upstream_revision": UPSTREAM_REVISION,
            "config": self.config.model_dump(mode="json"), "status": status,
            "evidence": evidence.model_dump(mode="json") if evidence else None,
            "session": session, "calls": calls, "events": events, "changes": changes,
            "usage": usage.model_dump(mode="json"), "error": error.model_dump(mode="json") if error else None}
        # Upstream attaches sets while aggregating. Persist a deterministic JSON representation.
        content = json.dumps(receipt, ensure_ascii=False, indent=2, default=lambda value: sorted(value) if isinstance(value, set) else str(value))
        ref = self.artifacts.put(content.encode("utf-8"), "skill_evolution")
        feedback = ConstructionFeedback(component="skill", operator_id=self.plugin_id, outcome=status,
            issues=issues, record_ref=ref)
        return EvolutionResponse(candidates=candidates, usage=usage, status=status, error=error,
                                 record_ref=ref, construction_feedback=feedback)
