"""Public output-contract adapter; never consumes private evaluation specs."""

from plugins.json_output import parse_final


class PublicJsonOutput:
    def schema(self, task):
        explicit = task.model_dump(mode="json")["input"].get("output_schema")
        if explicit is not None:
            if not isinstance(explicit, dict) or explicit.get("type") != "object":
                raise ValueError("public output_schema must describe a JSON object")
            return dict(explicit)
        # The current benchmark declares a small, public field/type vocabulary.
        # Keep this translation outside the Codex process/runtime implementation.
        format_spec = task.input.get("output_format")
        if format_spec is None:
            return None
        fields = format_spec.get("fields", ())
        properties = {}
        for field in fields:
            name, kind = field["name"], field["type"]
            if name in properties or kind not in {"number", "integer", "string", "boolean"}:
                raise ValueError("invalid public output field")
            properties[name] = {"type": kind}
        if not properties:
            raise ValueError("public output format must contain fields")
        return {"type": "object", "properties": properties, "required": list(properties),
                "additionalProperties": False}

    def parse(self, text):
        return parse_final(text)
