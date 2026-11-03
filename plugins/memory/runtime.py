"""Read a specific bank version, retrieve experiences and render injection evidence."""

from dataclasses import replace
import os
from dotenv import dotenv_values

from aer_core.errors import AERError
from .embedding import EmbeddingProvider, normalize
from .library import decode_bank, task_query


RUNTIME_VERSION = "reasoningbank-memory-v1"
STATE_FIELDS = ("memory_library_artifact_id", "memory_snapshot_version_id", "memory_retrieval", "memory_experience_count")


class MemoryRuntime:
    def __init__(self, config, artifacts, *, embedder=None):
        self.config, self.artifacts, self.embedder = config, artifacts, embedder
        # Derived vectors only. Membership is always read from the supplied Snapshot.
        self.vectors = {}

    def runtime_info(self):
        config = self.config.embedding
        values = dotenv_values(config.dotenv_path, interpolate=False) if config.dotenv_path else {}
        return {"version": RUNTIME_VERSION, "embedding_model": config.model_id,
            "embedding_endpoint": os.environ.get(config.base_url_env, values.get(config.base_url_env)),
            "dimensions": config.dimensions, "top_k": self.config.top_k}

    def validate(self, snapshot):
        ref = snapshot.artifacts.get("memory")
        if ref is None or ref.kind != "memory":
            raise ValueError("Memory requires a frozen memory artifact")
        return decode_bank(self.artifacts.get(ref))

    def _vector(self, text, run_ctx, record):
        if text not in self.vectors:
            if self.embedder is None:
                self.embedder = EmbeddingProvider(self.config.embedding)
            try:
                vector, usage = self.embedder.embed(text, run_ctx)
            except AERError as error:
                if error.to_info().usage is not None:
                    record.add_usage(error.to_info().usage)
                else:
                    record.unknown_call(model=True)
                raise
            record.add_usage(usage)
            self.vectors[text] = normalize(vector)
        return self.vectors[text]

    def retrieve(self, task, snapshot, run_ctx, record):
        bank = self.validate(snapshot)
        state = {"memory_library_artifact_id": snapshot.artifacts["memory"].artifact_id,
            "memory_snapshot_version_id": snapshot.version_id, "memory_experience_count": len(bank), "memory_retrieval": []}
        if not bank:
            record.event("memory_retrieval", state)
            return "", state
        query = task_query(task)
        ctx = replace(run_ctx, operation_id=run_ctx.operation_id + "/memory/embedding")
        # Keep the upstream instruction-aware query against historical task-query embeddings.
        instruction = "Given the prior task queries, analyze the current query's intent and select relevant prior queries that could help resolve it."
        query_vector = self._vector(f"Instruct: {instruction}\nQuery: {query}", ctx, record)
        scored = []
        for index, entry in enumerate(bank):
            vector = self._vector(entry["query"], ctx, record)
            if len(vector) != len(query_vector):
                raise ValueError("Query and memory embedding dimensions differ")
            score = sum(a * b for a, b in zip(query_vector, vector))
            scored.append((score, index))
        # AER can retry the same task: equal query embeddings prefer the latest
        # experience, so an appended lesson can actually participate in validation.
        selected = sorted(scored, key=lambda item: (-item[0], -item[1]))[:self.config.top_k]
        for score, index in selected:
            entry = bank[index]
            state["memory_retrieval"].append({"entry_index": index, "task_id": entry["task_id"],
                "similarity": score, "memory_items": entry["memory_items"]})
        content = "\n\n".join(part for hit in state["memory_retrieval"] for part in hit["memory_items"])
        injected = "\n\nReasoningBank memories from past task experiences:\n" + content + "\nUse these insights when relevant to the current task.\n"
        record.event("memory_retrieval", {**state, "injected_text": injected})
        return injected, state
