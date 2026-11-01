"""Accept an unambiguous JSON envelope at the review boundary, then parse strictly."""

import json

from plugins.json_output import parse_final


REVIEW_OUTPUT_VERSION = "review.json.v1"
JSON_OUTPUT_INSTRUCTION = (
    "Return exactly one bare JSON object. Do not use Markdown code fences, an explanatory preface, "
    "or trailing commentary. Use valid JSON escaping in strings; do not double-escape source newlines."
)


class ReviewOutputError(ValueError):
    def __init__(self, reason, message, **details):
        self.reason = reason
        self.issue = {"code": "review_invalid_json", "reason": reason, "message": message,
                      "hint": JSON_OUTPUT_INSTRUCTION, **details}
        super().__init__(reason)


def parse_review_output(content, receipt):
    """Strip only a whole-response single fence; do not extract/repair JSON or source strings."""
    if not isinstance(content, str):
        raise ReviewOutputError("missing_json_content", "Response content must be a JSON object string.")
    text = content.strip()
    receipt["output_normalization"] = {"version": REVIEW_OUTPUT_VERSION, "wrapper": "none"}
    if text.startswith("```"):
        lines = text.splitlines(keepends=True)
        if (len(lines) < 3 or lines[0].strip().lower() not in {"```json", "```"}
                or lines[-1].strip() != "```"
                or any(line.strip().startswith("```") for line in lines[1:-1])):
            raise ReviewOutputError("invalid_json_fence",
                "Only one complete JSON code block occupying the entire response is accepted; "
                "remove Markdown, extra blocks and surrounding prose.")
        # Keep payload bytes (including JSON escapes and source indentation) intact.
        text = "".join(lines[1:-1])
        receipt["output_normalization"]["wrapper"] = "single_json_fence"
    try:
        return parse_final(text)
    except json.JSONDecodeError as failure:
        raise ReviewOutputError("json_syntax_error", failure.msg, line=failure.lineno,
            column=failure.colno, position=failure.pos, coordinate_space="json_body") from failure
    except (ValueError, TypeError) as failure:
        # Retain strict duplicate-key, finite-number and object-root validation.
        raise ReviewOutputError("strict_json_violation", str(failure)[:240]) from failure
