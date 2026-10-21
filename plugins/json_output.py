"""Strict JSON-object parsing shared by plugins, independent of any Agent."""

import json

from pydantic import TypeAdapter

from aer_core.schemas.base import JsonObject


def parse_final(content: str | None) -> dict:
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError(f"duplicate output key: {key}")
            result[key] = value
        return result

    def invalid(value):
        raise ValueError(f"nonfinite output number: {value}")

    if content is None:
        raise ValueError("final response has no content")
    result = json.loads(content, object_pairs_hook=pairs, parse_constant=invalid)
    if not isinstance(result, dict):
        raise ValueError("final response must be a JSON object")
    TypeAdapter(JsonObject).validate_python(result)
    return result
