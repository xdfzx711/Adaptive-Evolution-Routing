"""Model diagnosis over public evidence, with structured hypotheses and call receipts."""

import json
from hashlib import sha256
from datetime import datetime, timezone
from time import monotonic

from aer_core.errors import AERError, ContractError, DiagnosisError
from aer_core.schemas.base import DomainModel, Identifier, Items, NonNegativeInt, Score
from aer_core.schemas.diagnosis import DiagnosisRecord
from aer_core.schemas.execution import Diagnosis, DiagnosisStatement
from aer_core.schemas.scoring import EvidenceReference
from aer_core.schemas.providers import ModelMessage, ModelRequest, ModelResponse
from plugins.agents.execution import SessionRecord
from plugins.json_output import parse_final
from .lightweight import LightweightDiagnoser
from .prompt import DIAGNOSIS_VERSION, SYSTEM_PROMPT
from aer_core.diagnostic_evidence import validate_references


class DiagnosisAnalysis(DomainModel):
    reason_code: Identifier
    explanation: Identifier
    observations: Items[DiagnosisStatement]
    hypotheses: Items[DiagnosisStatement]
    evidence_step_ids: Items[NonNegativeInt]
    confidence: Score | None = None
    unknowns: Items[str] = ()


class Observation(DomainModel):
    text: Identifier
    evidence_refs: Items[EvidenceReference]


class Hypothesis(Observation):
    verification: Identifier


class DiagnosticAnalysis(DomainModel):
    explanation: Identifier
    observations: Items[Observation]
    hypotheses: Items[Hypothesis]
    unknowns: Items[str]


class LLMDiagnoser:
    plugin_id = "llm_diagnoser"

    def __init__(self, model, artifacts):
        self.model, self.artifacts = model, artifacts

    def evidence_key(self, task, result, feedback):
        payload = {"prompt": SYSTEM_PROMPT, "version": DIAGNOSIS_VERSION,
                   "task": task.model_dump(mode="json"), "execution": result.model_dump(mode="json"),
                   "feedback": feedback.model_dump(mode="json")}
        return sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()).hexdigest()

    def diagnose(self, task, result, feedback, run_ctx) -> Diagnosis:
        if task.task_id != result.task_id:
            raise ContractError("Diagnosis task and execution must match", stage="diagnosis")
        if feedback.success:
            return LightweightDiagnoser().diagnose(task, result, feedback, run_ctx)
        started_at = datetime.now(timezone.utc)
        record = SessionRecord()
        allowed_reasons = {feedback.reason_code, "no_failure", "step_limit", "invalid_output_format",
                           "tool_error", "incorrect_answer", "unknown_failure"}
        response, diagnosis, error = None, None, None
        request = ModelRequest(messages=[
            ModelMessage(role="system", content=SYSTEM_PROMPT),
            ModelMessage(role="user", content=json.dumps({"operation": "failure_diagnosis",
                "task": task.model_dump(mode="json"), "execution": result.model_dump(mode="json"),
                "feedback": feedback.model_dump(mode="json"),
                "diagnosis_schema": DiagnosticAnalysis.model_json_schema()}, ensure_ascii=False))],
            tools=[], seed=run_ctx.seed, max_output_tokens=run_ctx.max_output_tokens)
        try:
            begin = monotonic()
            try:
                value = self.model.generate(request, run_ctx)
            except Exception as failure:
                if isinstance(failure, AERError) and failure.to_info().usage is not None:
                    record.add_usage(failure.to_info().usage)
                else:
                    record.unknown_call(model=True)
                raise
            if not isinstance(value, ModelResponse):
                record.unknown_call(model=True)
                raise ValueError("ModelProvider did not return ModelResponse")
            response = value
            record.add_usage(response.usage)
            if monotonic() - begin >= run_ctx.model_timeout_seconds:
                raise ValueError("Model diagnosis exceeded its call timeout")
            if response.tool_calls or response.usage.tool_calls or response.usage.model_calls < 1:
                raise ValueError("Diagnosis must report a model call without tool calls")
            if response.finish_reason == "length":
                raise ValueError("Model diagnosis reached the output token limit")
            raw = parse_final(response.content)
            legacy = isinstance(raw, dict) and "reason_code" in raw
            if legacy:
                parsed = DiagnosisAnalysis.model_validate(raw)
                if parsed.reason_code not in allowed_reasons or parsed.reason_code == "no_failure":
                    raise ValueError("Invalid legacy diagnostic category")
                values = parsed.model_dump(mode="json", exclude={"reason_code", "confidence"})
            else:
                parsed = DiagnosticAnalysis.model_validate(raw)
                if any(not item.evidence_refs for item in parsed.observations):
                    raise ValueError("Each observation must cite public evidence")
                if any(not item.verification or not item.verification.strip() for item in parsed.hypotheses):
                    raise ValueError("Each hypothesis must explain how it can be verified")
                values = parsed.model_dump(mode="json")
            diagnosis = Diagnosis(**values, reason_code=feedback.reason_code, source="model", usage=record.usage(),
                diagnosis_version=DIAGNOSIS_VERSION, evidence_key=self.evidence_key(task, result, feedback))
            validate_references(diagnosis, task, result, feedback)
        except Exception as failure:
            diagnosis = None
            message = ("Model diagnosis failed to satisfy its evidence or JSON contract"
                if not isinstance(failure, AERError) else "Model diagnosis call failed: " + failure.to_info().code)
            error = DiagnosisError(message, stage="diagnosis", operation_id=run_ctx.operation_id,
                plugin_id=self.plugin_id, usage=record.usage()).to_info()
        usage = record.usage()
        if diagnosis:
            diagnosis = diagnosis.replace(usage=usage)
        receipt = DiagnosisRecord(run_id=run_ctx.run_id, episode_id=run_ctx.episode_id, task_id=task.task_id,
            operation_id=run_ctx.operation_id, started_at=started_at, finished_at=datetime.now(timezone.utc),
            task=task, execution=result, feedback=feedback, response=response, diagnosis=diagnosis, usage=usage, error=error)
        try:
            ref = self.artifacts.put(receipt.model_dump_json(indent=2).encode("utf-8"), "diagnosis")
        except Exception as failure:
            raise DiagnosisError("Cannot save model diagnosis evidence", stage="diagnosis", operation_id=run_ctx.operation_id,
                plugin_id=self.plugin_id, usage=usage) from failure
        if error:
            raise DiagnosisError(error.message, stage="diagnosis", operation_id=run_ctx.operation_id,
                plugin_id=self.plugin_id, usage=usage, evidence_ref=ref)
        return diagnosis.replace(record_ref=ref)
