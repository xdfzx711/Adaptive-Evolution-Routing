"""ReasoningBank SWE JSONL: query, task_id, status, and Markdown memory_items."""

import json
import re
from pathlib import Path

from aer_core.config.models import NonBlank, PositiveFloat, PositiveInt, Settings


class EmbeddingConfig(Settings):
    model_id: NonBlank
    api_key_env: NonBlank = "MEMORY_EMBEDDING_API_KEY"
    base_url_env: NonBlank = "MEMORY_EMBEDDING_BASE_URL"
    dotenv_path: NonBlank | None = None
    dimensions: PositiveInt | None = None
    timeout_seconds: PositiveFloat = 60.0


class MemoryConfig(Settings):
    initial_library: NonBlank | None = None
    top_k: PositiveInt = 1
    embedding: EmbeddingConfig

    def controlled(self):
        return self.model_dump(mode="json", exclude={"initial_library"})


def decode_bank(content: bytes) -> list[dict]:
    """Preserve upstream entries, including its optional trajectory fields."""
    entries = []
    for line in content.decode("utf-8-sig").splitlines():
        if not line.strip():
            continue
        item = json.loads(line)
        if not isinstance(item, dict) or type(item.get("task_id")) not in (str, int):
            raise ValueError("Memory entry requires task_id")
        if not isinstance(item.get("query"), str) or not item["query"].strip():
            raise ValueError("Memory entry requires a nonempty query")
        if item.get("status") not in {"success", "fail"}:
            raise ValueError("Memory status must be success or fail")
        parts = item.get("memory_items")
        if not isinstance(parts, list) or not parts or any(not isinstance(part, str) or not part.strip() for part in parts):
            raise ValueError("ReasoningBank memory_items must be a nonempty list of Markdown strings")
        entries.append(item)
    return entries


def encode_bank(entries: list[dict]) -> bytes:
    content = "".join(json.dumps(item, ensure_ascii=False, allow_nan=False) + "\n" for item in entries).encode("utf-8")
    decode_bank(content)
    return content


def initial_memory_artifacts(settings):
    if settings is None:
        return {}
    content = Path(settings.initial_library).read_bytes() if settings.initial_library else b""
    return {"memory": encode_bank(decode_bank(content))}


def task_query(task) -> str:
    # SWE uses the issue text; generic tasks retain all public input fields.
    for key in ("problem_statement", "query", "intent"):
        value = task.input.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return json.dumps(task.model_dump(mode="json")["input"], ensure_ascii=False, sort_keys=True)


def parse_items(text: str) -> list[str]:
    """Validate complete items; preserve Markdown rather than inventing a new format."""
    text = text.strip()
    if text == "[]":
        return []
    if text.startswith("```") and text.endswith("```"):
        text = text.split("\n", 1)[1].rsplit("```", 1)[0].strip()
    starts = list(re.finditer(r"(?m)^# Memory Item \d+\s*$", text))
    if not starts or text[:starts[0].start()].strip() or len(starts) > 3:
        raise ValueError("Expected one to three complete Markdown memory items, or []")
    result = []
    for index, start in enumerate(starts):
        block = text[start.start():starts[index + 1].start() if index + 1 < len(starts) else len(text)].strip()
        match = re.fullmatch(r"# Memory Item \d+\s*\n## Title\s+(.+?)\n## Description\s+(.+?)\n## Content\s+(.+)", block, re.S)
        if not match or not all(part.strip() for part in match.groups()):
            raise ValueError("Every memory item requires title, description and content")
        result.append(block)
    return result
