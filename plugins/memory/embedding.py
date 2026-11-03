"""Configurable OpenAI-compatible embeddings with explicit resource accounting."""

import math
import os
from time import monotonic
from urllib.parse import urlsplit

import httpx
from dotenv import dotenv_values

from aer_core.errors import ExecutionError
from aer_core.schemas.usage import ResourceUsage


def normalize(vector):
    if not isinstance(vector, (list, tuple)) or not vector or any(type(x) not in (int, float) or not math.isfinite(x) for x in vector):
        raise ValueError("Embedding must be a finite numeric vector")
    norm = math.sqrt(sum(x * x for x in vector))
    if not math.isfinite(norm) or norm == 0:
        raise ValueError("Embedding must have a finite nonzero norm")
    return tuple(x / norm for x in vector)


class EmbeddingProvider:
    def __init__(self, config, *, transport=None):
        self.config, self.transport = config, transport
        if config.model_id.startswith("${"):
            raise ValueError("Resolve the embedding model environment variable before execution")
        values = dotenv_values(config.dotenv_path, interpolate=False) if config.dotenv_path else {}
        self.key = os.environ.get(config.api_key_env, values.get(config.api_key_env))
        base = os.environ.get(config.base_url_env, values.get(config.base_url_env))
        if not self.key or not self.key.strip() or not base or not base.strip():
            raise ValueError(f"Missing embedding settings: {config.api_key_env}, {config.base_url_env}")
        base = base.strip()
        parts = urlsplit(base)
        if parts.scheme not in {"https", "http"} or not parts.hostname or parts.username or parts.password or parts.query or parts.fragment:
            raise ValueError("Embedding base URL must be HTTP(S) without credentials, query or fragment")
        self.key = self.key.strip()
        base = base.strip().rstrip("/")
        self.endpoint = base if base.endswith("/embeddings") else base + "/embeddings"

    def embed(self, text, run_ctx):
        start = monotonic()
        usage = ResourceUsage.zero()
        try:
            usage = ResourceUsage(input_tokens=None, output_tokens=0, total_tokens=None, model_calls=1,
                tool_calls=0, elapsed_seconds=0, usage_complete=False, usage_sources=["unknown"])
            payload = {"model": self.config.model_id, "input": [text], "encoding_format": "float"}
            if self.config.dimensions is not None:
                payload["dimensions"] = self.config.dimensions
            with httpx.Client(transport=self.transport, timeout=min(self.config.timeout_seconds, run_ctx.model_timeout_seconds)) as client:
                response = client.post(self.endpoint, headers={"Authorization": "Bearer " + self.key}, json=payload)
                response.raise_for_status()
                data = response.json()
            tokens = data.get("usage", {}).get("prompt_tokens", data.get("usage", {}).get("total_tokens"))
            known = type(tokens) is int and tokens >= 0
            usage = usage.replace(input_tokens=tokens if known else None, total_tokens=tokens if known else None,
                usage_complete=known, usage_sources=["provider_reported" if known else "unknown"])
            rows = data["data"]
            if len(rows) != 1 or rows[0].get("index", 0) != 0:
                raise ValueError("Embedding response must contain exactly one matching row")
            vector = normalize(rows[0]["embedding"])
            if self.config.dimensions is not None and len(vector) != self.config.dimensions:
                raise ValueError("Embedding dimension differs from configuration")
            return vector, usage.replace(elapsed_seconds=monotonic() - start)
        except Exception as error:
            # HTTP exception strings may include an endpoint or response body.
            raise ExecutionError("Memory embedding request failed (" + type(error).__name__ + ")",
                stage=run_ctx.stage, operation_id=run_ctx.operation_id, plugin_id="memory_embedding",
                usage=usage.replace(elapsed_seconds=monotonic() - start)) from None
