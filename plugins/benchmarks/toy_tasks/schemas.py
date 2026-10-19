"""The small benchmark's public format and private scoring contract."""

from collections.abc import Mapping
from math import isfinite
from typing import Literal

from pydantic import model_validator

from aer_core.schemas.base import DomainModel, FiniteFloat, Identifier, Items, JsonObject, NonNegativeFloat


class OutputField(DomainModel):
    name: Identifier
    type: Literal["number", "integer", "string", "boolean"]


class OutputFormat(DomainModel):
    fields: Items[OutputField]
    allow_extra_fields: bool

    @model_validator(mode="after")
    def unique_fields(self):
        names = [field.name for field in self.fields]
        if not names or len(names) != len(set(names)):
            raise ValueError("output fields must be nonempty and unique")
        return self


class ToyInput(DomainModel):
    instruction: Identifier
    data: JsonObject
    output_format: OutputFormat
    numeric_tolerance: NonNegativeFloat | None = None


class PublicTask(DomainModel):
    task_id: Identifier
    source_id: Identifier
    split: Literal["dev", "validation", "test"]
    category: Literal["arithmetic", "extraction", "formatting"]
    family: Identifier | None = None
    validation_group: Literal["similar", "regression"] | None
    input: ToyInput

    @model_validator(mode="after")
    def grouping(self):
        if (self.split == "validation") != (self.validation_group is not None):
            raise ValueError("only validation tasks must have a validation_group")
        return self


class PublicDataset(DomainModel):
    benchmark_id: Identifier
    dataset_version: Identifier
    tasks: Items[PublicTask]


class PrivateScoringEntry(DomainModel):
    task_id: Identifier
    scoring_method: Literal["numeric_answer", "structured_answer"]
    private_parameters: JsonObject


class PrivateDataset(DomainModel):
    benchmark_id: Identifier
    dataset_version: Identifier
    scorer_version: Literal["toy.scoring.v1"]
    specs: Items[PrivateScoringEntry]


class NumericParameters(DomainModel):
    expected: int | FiniteFloat
    absolute_tolerance: NonNegativeFloat


class StructuredParameters(DomainModel):
    expected: JsonObject


def output_has_format(output: Mapping, format_spec: OutputFormat) -> bool:
    names = {field.name for field in format_spec.fields}
    if not names.issubset(output) or (not format_spec.allow_extra_fields and set(output) != names):
        return False
    for field in format_spec.fields:
        value = output[field.name]
        if field.type == "number":
            valid = type(value) is int or (type(value) is float and isfinite(value))
        elif field.type == "integer":
            valid = type(value) is int
        elif field.type == "boolean":
            valid = type(value) is bool
        else:
            valid = type(value) is str
        if not valid:
            return False
    return True


def validate_scoring(input_spec: ToyInput, method: str, parameters: Mapping):
    """Validate the rubric against the requirements actually visible to an Agent."""
    if method == "numeric_answer":
        parsed = NumericParameters.model_validate(dict(parameters))
        fields = input_spec.output_format.fields
        if len(fields) != 1 or fields[0].name != "answer" or fields[0].type not in {"number", "integer"}:
            raise ValueError("numeric_answer requires a single numeric answer field")
        if input_spec.numeric_tolerance != parsed.absolute_tolerance:
            raise ValueError("numeric tolerance must be declared in public input")
        if not output_has_format({"answer": parsed.expected}, input_spec.output_format):
            raise ValueError("expected numeric answer does not match public output format")
        return parsed
    if method == "structured_answer":
        parsed = StructuredParameters.model_validate(dict(parameters))
        if input_spec.numeric_tolerance is not None:
            raise ValueError("structured_answer does not use numeric tolerance")
        declared = {field.name for field in input_spec.output_format.fields}
        if set(parsed.expected) != declared or not output_has_format(parsed.expected, input_spec.output_format):
            raise ValueError("expected fields do not match public output format")
        return parsed
    raise ValueError(f"unsupported scoring method: {method}")
