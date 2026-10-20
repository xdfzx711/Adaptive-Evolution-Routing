"""Reuse one complete task execution per Snapshot, seed and assessment conditions."""

import json
from hashlib import sha256

from aer_core.errors import ContractError
from aer_core.contracts.validation import ValidationCache, ValidationTaskExecutor
from aer_core.schemas.paired import ScoredExecution, ValidationEvidence
from aer_core.schemas.references import AgentSnapshot
from aer_core.schemas.runtime import RunContext
from aer_core.schemas.task import Task
from aer_core.schemas.usage import ResourceUsage


class CachedTaskExecutor:
    def __init__(self, executor: ValidationTaskExecutor, cache: ValidationCache, conditions: dict):
        self.executor, self.cache = executor, cache
        self.conditions = ValidationEvidence(conditions=conditions, results=[])

    def execute(self, task: Task, snapshot: AgentSnapshot, run_ctx: RunContext) -> ScoredExecution:
        data = {"snapshot": snapshot.model_dump(mode="json"), "task": task.model_dump(mode="json"),
                "seed": run_ctx.seed, "conditions": {**self.conditions.model_dump(mode="json")["conditions"],
                    "model_timeout_seconds": run_ctx.model_timeout_seconds,
                    "tool_timeout_seconds": run_ctx.tool_timeout_seconds,
                    "max_output_tokens": run_ctx.max_output_tokens}}
        key = sha256(json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                                allow_nan=False).encode("utf-8")).hexdigest()
        result = self.cache.get(key)
        hit = result is not None
        if not hit:
            result = self.executor.execute(task, snapshot, run_ctx)
        if not isinstance(result, ScoredExecution):
            raise ContractError("Executor/cache must return ScoredExecution", stage=run_ctx.stage)
        if result.task != task.identity or result.version_id != snapshot.version_id or result.seed != run_ctx.seed:
            raise ContractError("Execution/cache identities differ from task/version/seed", stage=run_ctx.stage,
                                usage=ResourceUsage.zero() if hit else result.charged_usage)
        if hit:
            if not result.complete:
                raise ContractError("Cached execution is incomplete", stage=run_ctx.stage)
            return result.replace(cache_hit=True, reuse_operation_id=run_ctx.operation_id)
        if result.complete:
            self.cache.put(key, result.replace(cache_hit=False, reuse_operation_id=None))
        return result
