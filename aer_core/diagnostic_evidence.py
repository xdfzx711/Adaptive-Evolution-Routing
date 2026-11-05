"""Check references against the public inputs; render compact model-facing diagnoses."""

from aer_core.errors import DiagnosisError


FEEDBACK_GUIDANCE = (
    "Benchmark feedback metrics, scoring_rules and deterministic findings are authoritative for the recorded evaluation. "
    "Diagnosis observations cite public evidence; hypotheses require verification and are not expected answers. "
    "Do not contradict exact-match or precision/recall findings, invent hidden reference labels, or assume undocumented "
    "scoring semantics. Respect the task constraints and acknowledge unavailable evidence. "
)


def resolve_pointer(document, pointer):
    current = document
    for raw in pointer.split("/")[1:]:
        if "~" in raw.replace("~0", "").replace("~1", ""):
            raise ValueError("Invalid JSON pointer escape")
        token = raw.replace("~1", "/").replace("~0", "~")
        if isinstance(current, list):
            if not token.isdigit() or str(int(token)) != token:
                raise ValueError("Invalid array index")
            current = current[int(token)]
        elif isinstance(current, dict):
            current = current[token]
        else:
            raise ValueError("Reference traverses a scalar")
    return current


def validate_references(diagnosis, task, execution, feedback):
    sources = {"task": task.model_dump(mode="json"), "execution": execution.model_dump(mode="json"),
               "evaluation": feedback.model_dump(mode="json"),
               "scoring_rules": feedback.scoring_rules.model_dump(mode="json") if feedback.scoring_rules else None}
    if not diagnosis.all_evidence_step_ids <= {event.seq for event in execution.trajectory}:
        raise DiagnosisError("Diagnosis references an unavailable trajectory event", stage="diagnosis")
    for statement in (*diagnosis.observations, *diagnosis.hypotheses):
        for ref in statement.evidence_refs:
            try:
                if resolve_pointer(sources[ref.source], ref.path) is None:
                    raise ValueError("Unavailable evidence")
            except (KeyError, IndexError, TypeError, ValueError) as failure:
                raise DiagnosisError("Diagnosis references unavailable public evidence: " + ref.source + ref.path,
                                     stage="diagnosis") from failure
    # Reference existence does not establish arbitrary natural-language causality.
    return diagnosis


def diagnosis_payload(diagnosis):
    if diagnosis is None:
        return None
    payload = diagnosis.model_dump(mode="json", exclude={"usage", "record_ref", "evidence_key", "confidence"})
    # Existing source-review findings are substantive evidence, not accounting.
    if payload["fact_check"] is None:
        del payload["fact_check"]
    return payload


def history_payload(history):
    return [dict(item.model_dump(mode="json"), diagnosis=diagnosis_payload(item.diagnosis)) for item in history]


def evolution_context_payload(context):
    payload = context.model_dump(mode="json", exclude={"stable_snapshot", "run_context"})
    payload["diagnosis"] = diagnosis_payload(context.diagnosis)
    payload["history"] = history_payload(context.history)
    return payload
