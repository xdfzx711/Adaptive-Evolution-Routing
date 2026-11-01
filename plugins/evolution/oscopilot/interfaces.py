"""Public JSON contracts carried beside the unchanged native ActionNode graph."""

import jsonschema
import re
from typing import Literal
from pydantic import model_validator

from aer_core.schemas.base import DomainModel, JsonObject
from plugins.tools.generated.paths import repository_path_fields
from plugins.tools.generated.contracts import ToolUsage, RuntimeOutcome


def validate_json_schema(schema, name):
    try:
        jsonschema.Draft202012Validator.check_schema(schema)
    except jsonschema.SchemaError as failure:
        raise ValueError(f"{name} must use JSON types, not Python runtime objects: {failure.message}") from failure
    if "type" not in schema:
        raise ValueError(f"{name} must explicitly describe its JSON type")
    if name == "input_schema":
        repository_path_fields(schema)
        if schema["type"] != "object":
            raise ValueError("Public tool arguments must be a JSON object")
        if "workspace" in schema.get("properties", {}) or "workspace" in schema.get("required", []):
            raise ValueError("workspace is runtime-injected, not a public interface argument")


class DeliveryAcceptance(DomainModel):
    """Success on the representative delivery example, separate from the general output type."""

    kind: Literal["json", "patch"]
    success_schema: JsonObject
    patch_pointer: str | None = None

    @model_validator(mode="after")
    def valid_acceptance(self):
        schema = self.model_dump(mode="json")["success_schema"]
        validate_json_schema(schema, "success_schema")
        if not (set(schema) - {"type", "title", "description", "$schema", "$comment", "examples", "default"}):
            raise ValueError("success_schema must assert a successful example result, not only its JSON type")
        if self.kind == "patch":
            if self.patch_pointer is None or (self.patch_pointer and not self.patch_pointer.startswith("/")):
                raise ValueError("Patch acceptance requires patch_pointer (JSON Pointer; empty string selects the root)")
            if re.search(r"~(?![01])", self.patch_pointer):
                raise ValueError("patch_pointer must use valid JSON Pointer escaping")
        elif self.patch_pointer is not None:
            raise ValueError("patch_pointer is only valid for patch acceptance")
        return self


class JSONToolInterface(DomainModel):
    input_schema: JsonObject
    output_schema: JsonObject
    acceptance: DeliveryAcceptance
    usage: ToolUsage | None = None
    runtime_outcome: RuntimeOutcome | None = None

    @model_validator(mode="after")
    def json_boundary(self):
        schemas = self.model_dump(mode="json")
        for name in ("input_schema", "output_schema"):
            validate_json_schema(schemas[name], name)
        return self
