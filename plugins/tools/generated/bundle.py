"""Self-contained tool artifacts: restoring a Snapshot restores its implementations."""

import ast
import json
import re
from typing import Literal

import jsonschema
from pydantic import Field, model_validator

from aer_core.schemas.base import DomainModel, Identifier, JsonObject, ReadOnlyMap
from plugins.tool_selection import ToolDescriptor, ToolSelection
from plugins.json_output import parse_final
from .paths import repository_path_fields
from .contracts import ToolUsage, RuntimeOutcome


def valid_name(value):
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,63}", value):
        raise ValueError("Generated tool names must be Python identifiers of 1-64 characters")
    return value


class GeneratedTool(DomainModel):
    description: Identifier
    source: Identifier
    entrypoint: Identifier
    kind: Literal["function", "class"] = "function"
    input_schema: JsonObject
    constructor_arguments: JsonObject = Field(default_factory=dict)
    version: Identifier = "1"
    usage: ToolUsage | None = None
    usage_example: JsonObject = Field(default_factory=dict)
    output_schema: JsonObject | None = None
    runtime_outcome: RuntimeOutcome | None = None

    @model_validator(mode="after")
    def valid_implementation(self):
        valid_name(self.entrypoint)
        tree = ast.parse(self.source)
        expected = ast.FunctionDef if self.kind == "function" else ast.ClassDef
        node = next((item for item in tree.body if isinstance(item, expected) and item.name == self.entrypoint), None)
        if node is None:
            raise ValueError("Generated source must define the declared synchronous entrypoint")
        schema = self.model_dump(mode="json")["input_schema"]
        jsonschema.Draft202012Validator.check_schema(schema)
        repository_path_fields(schema)
        if schema.get("type") != "object":
            raise ValueError("Tool input_schema must describe an object")
        if "workspace" in schema.get("properties", {}):
            raise ValueError("workspace is injected by the runtime, not a public argument")
        if self.kind == "function" and self.constructor_arguments:
            raise ValueError("Only class tools have constructor arguments")
        if self.output_schema is not None:
            jsonschema.Draft202012Validator.check_schema(data_schema := self.model_dump(mode="json")["output_schema"])
            if "type" not in data_schema:
                raise ValueError("Output schema needs an explicit JSON type")
        if self.runtime_outcome is not None and self.output_schema is None:
            raise ValueError("Runtime outcomes require an output schema")
        if self.usage is not None:
            jsonschema.validate(self.model_dump(mode="json")["usage_example"], schema)
        return self

    def descriptor(self):
        description = self.description
        if self.usage is not None:
            description += "\n" + "\n".join(f"{key}: {value}" for key, value in self.usage.model_dump().items())
            description += "\nJSON example (adapt to the current task inputs): " + json.dumps(self.model_dump(mode="json")["usage_example"], ensure_ascii=False)
        return ToolDescriptor(description=description, version=self.version,
                              input_schema=self.model_dump(mode="json")["input_schema"])


class ToolBundle(ToolSelection):
    schema_version: Literal["aer.tools.v1"] = "aer.tools.v1"
    generated_tools: ReadOnlyMap[GeneratedTool] = Field(default_factory=dict)

    @model_validator(mode="after")
    def names(self):
        for name in self.generated_tools:
            valid_name(name)
        return self


def decode_tools(content):
    data = parse_final(content.decode("utf-8") if isinstance(content, bytes) else content)
    return ToolBundle.model_validate(data) if "schema_version" in data else ToolSelection.model_validate(data)


def generated_tools(selection):
    return selection.generated_tools if isinstance(selection, ToolBundle) else {}


def validate_tools(selection, catalog, *, allow_generated):
    generated = generated_tools(selection)
    if generated and not allow_generated:
        raise ValueError("Generated tools are not enabled by this Agent configuration")
    if set(generated) & set(catalog):
        raise ValueError("Generated tools cannot shadow configured static tools")
    if not set(selection.enabled_tools) <= (set(catalog) | set(generated)):
        raise ValueError("Selected tool has no implementation in this Snapshot")


def with_selection(selection, names):
    return selection.replace(enabled_tools=names)
