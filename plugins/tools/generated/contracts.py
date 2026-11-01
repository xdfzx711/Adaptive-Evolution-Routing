"""Published usage guidance and per-invocation outcomes, separate from delivery examples."""

import jsonschema
from pydantic import model_validator
from aer_core.schemas.base import DomainModel, Identifier, JsonObject


class ToolUsage(DomainModel):
    capability_gap: Identifier
    use_when: Identifier
    added_value: Identifier
    result_usage: Identifier
    preconditions: Identifier
    cost: Identifier


class RuntimeOutcome(DomainModel):
    success_schema: JsonObject
    failure_schema: JsonObject

    @model_validator(mode="after")
    def valid_schemas(self):
        for schema in self.model_dump(mode="json").values():
            try:
                jsonschema.Draft202012Validator.check_schema(schema)
            except jsonschema.SchemaError as error:
                raise ValueError("Invalid runtime outcome schema: " + error.message) from error
            if "type" not in schema or not set(schema) - {"type", "description", "title", "$schema"}:
                raise ValueError("Runtime outcome schemas require explicit types and outcome conditions")
        return self


def classify_result(tool, output):
    """Classification is observational; it never changes native judgment or the returned tool value."""
    if output.get("status") != "completed" or output.get("error") is not None:
        return {"business_status": "not_evaluated", "output_valid": None}
    data = tool.model_dump(mode="json")
    if data["output_schema"] is None or data["runtime_outcome"] is None:
        return {"business_status": "unknown", "output_valid": None}
    value = output.get("result")
    valid = jsonschema.Draft202012Validator(data["output_schema"]).is_valid(value)
    if not valid:
        return {"business_status": "failure", "output_valid": False, "outcome_reason": "output_schema_mismatch"}
    outcome = data["runtime_outcome"]
    success = jsonschema.Draft202012Validator(outcome["success_schema"]).is_valid(value)
    failure = jsonschema.Draft202012Validator(outcome["failure_schema"]).is_valid(value)
    return {"business_status": "success" if success and not failure else "failure" if failure and not success else "unknown",
        "output_valid": True, "outcome_reason": "ambiguous_outcome" if success and failure else
        "unclassified_outcome" if not success and not failure else "declared_outcome"}
