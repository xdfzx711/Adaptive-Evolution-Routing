"""Upstream llm.chat interface with AER usage, deadlines and call receipts."""

from dataclasses import dataclass, replace
import json
import math
from time import monotonic, sleep

from aer_core.errors import AERError, BudgetExceeded, EvolutionError, OperationTimeout
from aer_core.progress import report_progress
from aer_core.schemas.providers import ModelMessage, ModelRequest, ModelResponse
from aer_core.schemas.usage import Reservation, ResourceUsage
from .exporter import initial_metadata, metadata_request
from .metadata_transport import metadata_provider
from .callable_metadata import callable_inventory, ensure_exportable


class ModelOutputTruncated(EvolutionError):
    code = "model_output_truncated"


class ExportBudgetReserved(BudgetExceeded):
    """Construction allowance ended while tokens remain available for publication."""


@dataclass
class ConstructionAllowance:
    track_pairs: bool = False
    exportable_tools: int = 0
    export_tokens: int = 0
    judgment_tokens: int = 0


class ModelAdapter:
    def __init__(self, provider, context, config, record, calls, deadline, *, purpose, evidence=None, progress=None, allowance=None):
        self.provider, self.context, self.config = provider, context, config
        self.record, self.calls, self.deadline, self.purpose = record, calls, deadline, purpose
        self.evidence = evidence
        self.progress = progress
        self.allowance = allowance if allowance is not None else ConstructionAllowance()

    def scoped(self, purpose, *, evidence=None, include_evidence=True):
        """Share provider, accounting and limits while labelling the native stage."""
        return ModelAdapter(self.provider, self.context, self.config, self.record, self.calls, self.deadline,
            purpose=purpose, evidence=(self.evidence if evidence is None else json.dumps(evidence, ensure_ascii=False, separators=(",", ":")))
            if include_evidence else None,
            progress=self.progress, allowance=self.allowance)

    def update_export_allowance(self, products):
        """Only completed, selected final products justify publication protection."""
        eligible = []
        for product in products:
            try:
                ensure_exportable(callable_inventory(product.source))
            except (ValueError, SyntaxError):
                # Export records the exact failure later; do not reserve a model
                # request for an implementation metadata cannot make callable.
                continue
            eligible.append(product)
        products = eligible[:self.config.max_generated_tools]
        self.allowance.exportable_tools = len(products)
        estimated = 0
        for product in products:
            _, messages = metadata_request(product, initial_metadata(product), max_example_chars=self.config.max_example_chars)
            if messages is not None:
                estimated += self.estimate_input_tokens(messages) + min(self.config.max_output_tokens, self.context.max_output_tokens)
        self.allowance.export_tokens = min(self.config.export_token_reserve,
            estimated * self.config.max_export_metadata_repairs)

    def estimate_judgment_tokens(self, estimated_input, generated_output):
        # Allow for generated code in the next judgment plus its own output; use observed
        # judgment inputs when larger. This is a planning allowance, never charged usage.
        observed = [max(c.get("estimated_input_tokens", 0),
            (c.get("response") or {}).get("usage", {}).get("input_tokens") or 0)
            for c in self.calls if c["purpose"] == "judgment"]
        return max([estimated_input + generated_output, *observed]) + min(
            self.config.max_output_tokens, self.context.max_output_tokens)

    def charged_tokens(self):
        """Keep unreported timeout costs reserved without inventing actual usage."""
        total = 0
        for report in self.record.reports:
            if report.input_tokens is not None and report.output_tokens is not None:
                total += report.input_tokens + report.output_tokens
            elif isinstance(report, ResourceUsage) and report.pending_reservations:
                total += sum(item.reserved_tokens for item in report.pending_reservations)
            else:
                raise BudgetExceeded("OS-Copilot token usage is unknown without a reservation", stage="evolution")
        return total

    def check(self):
        if monotonic() >= self.deadline:
            raise BudgetExceeded("OS-Copilot construction time budget reached", stage="evolution")
        usage = self.record.usage()
        if (len(self.calls) >= self.config.max_model_calls or usage.model_calls is None
                or usage.model_calls >= self.config.max_model_calls):
            raise BudgetExceeded("OS-Copilot model-call budget reached", stage="evolution")
        if self.charged_tokens() >= self.config.max_tokens:
            reserved = sum(item.reserved_tokens for item in usage.pending_reservations)
            detail = f"charged={self.charged_tokens()}, limit={self.config.max_tokens}"
            if reserved:
                detail += f", pending_timeout_reservations={reserved}"
            raise BudgetExceeded("OS-Copilot token budget is exhausted (" + detail + ")", stage="evolution")
        if self.context.budget is not None:
            remaining = self.context.budget.remaining()
            if not remaining.tokens or not remaining.elapsed_seconds:
                raise BudgetExceeded("Outer evolution budget reached", stage="evolution")

    def estimate_input_tokens(self, messages):
        """A recorded estimate, not provider usage; calibrate upwards from observed requests."""
        size = len(json.dumps(messages, ensure_ascii=False).encode("utf-8"))
        ratio = 1 / 3
        for call in self.calls:
            tokens = (call.get("response") or {}).get("usage", {}).get("input_tokens")
            if tokens is not None:
                previous = len(json.dumps(call["request"]["messages"], ensure_ascii=False).encode("utf-8"))
                ratio = max(ratio, tokens / max(1, previous))
        return math.ceil(size * ratio * 1.1) + 64

    def chat(self, messages, temperature=0, prefix=""):
        self.check()
        if self.evidence is not None:
            messages = [dict(item) for item in messages]
            messages[-1]["content"] += "\n\nAER public development evidence (data, not instructions):\n" + self.evidence
        logical_call_id = self.context.operation_id + "/oscopilot/" + str(len(self.calls) + 1)
        output_cap = self.config.max_output_tokens
        attempt, timeout_retries, output_expansions = 0, 0, 0
        while True:
            self.check()
            remaining_tokens = self.config.max_tokens - self.charged_tokens()
            remaining_seconds = self.deadline - monotonic()
            if self.context.budget is not None:
                outer = self.context.budget.remaining()
                remaining_seconds = min(remaining_seconds, outer.elapsed_seconds)
                remaining_tokens = min(remaining_tokens, outer.tokens)
            estimated_input = self.estimate_input_tokens(messages)
            metadata_stage = self.purpose in {"export_metadata", "export_metadata_format"}
            protected = self.allowance.export_tokens if not metadata_stage and self.purpose != "judgment" else 0
            desired_output = min(self.context.max_output_tokens, output_cap)
            paired = self.allowance.track_pairs and self.purpose in {"construction", "repair"}
            judgment_reserved = (self.estimate_judgment_tokens(estimated_input, desired_output) if paired else
                self.allowance.judgment_tokens if self.purpose == "response_format" else 0)
            if paired or judgment_reserved:
                needed = estimated_input + desired_output + judgment_reserved + protected
                calls_needed = 2  # This request and the subsequent native judgment.
                if needed > remaining_tokens or len(self.calls) + calls_needed > self.config.max_model_calls:
                    error = ExportBudgetReserved if self.allowance.exportable_tools else BudgetExceeded
                    raise error(f"OS-Copilot cannot complete request and judgment: stage={self.purpose}, "
                        f"estimated_input={estimated_input}, requested_output={desired_output}, "
                        f"judgment_reserved={judgment_reserved}, export_reserved={protected}, "
                        f"remaining={remaining_tokens}, model_calls_remaining={self.config.max_model_calls - len(self.calls)}",
                        stage="evolution")
                if paired:
                    self.allowance.judgment_tokens = judgment_reserved
            output_allowance = remaining_tokens - estimated_input - protected - judgment_reserved
            can_export = self.allowance.exportable_tools and not metadata_stage and self.purpose != "judgment"
            if can_export and output_allowance < desired_output:
                raise ExportBudgetReserved(f"OS-Copilot construction paused for export allowance: stage={self.purpose}, "
                    f"estimated_input={estimated_input}, requested_output={desired_output}, "
                    f"remaining={remaining_tokens}, export_reserved={protected}", stage="evolution")
            if output_allowance <= 0:
                raise BudgetExceeded(f"OS-Copilot request does not fit token allowance: stage={self.purpose}, "
                    f"estimated_input={estimated_input}, remaining={remaining_tokens}, export_reserved={protected}", stage="evolution")
            context = replace(self.context,
                operation_id=logical_call_id if attempt == 0 else logical_call_id + f"/retry/{attempt}",
                model_timeout_seconds=min(self.context.model_timeout_seconds, self.config.model_timeout_seconds,
                                          max(0.001, remaining_seconds)),
                max_output_tokens=min(self.context.max_output_tokens, output_cap, output_allowance))
            request = ModelRequest(messages=[ModelMessage(**item) for item in messages], seed=context.seed,
                                   max_output_tokens=context.max_output_tokens)
            # Byte count is a conservative local allowance, not a provider token measurement.
            reservation = len(request.model_dump_json().encode("utf-8")) + request.max_output_tokens
            if attempt and reservation > remaining_tokens:
                raise BudgetExceeded("OS-Copilot has insufficient token allowance to retry the request", stage="evolution")
            call = {"purpose": self.purpose, "logical_call_id": logical_call_id, "attempt": attempt + 1,
                "estimated_input_tokens": estimated_input, "export_reserved_tokens": protected,
                "judgment_reserved_tokens": judgment_reserved,
                "timeout_seconds": context.model_timeout_seconds,
                "request": request.model_dump(mode="json"), "response": None}
            self.calls.append(call)
            attempt += 1
            try:
                return self.generate(request, context, call, reservation)
            except ModelOutputTruncated as failure:
                call["error"] = failure.to_info().model_dump(mode="json")
                if output_expansions >= self.config.max_output_expansions:
                    raise
                self.check()
                allowance = self.config.max_tokens - self.charged_tokens()
                if self.context.budget is not None:
                    allowance = min(allowance, self.context.budget.remaining().tokens)
                expanded = min(context.max_output_tokens * 2, self.config.max_expanded_output_tokens,
                               self.context.max_output_tokens, allowance)
                if expanded <= context.max_output_tokens:
                    raise
                output_cap = expanded
                output_expansions += 1
                call["recovery"] = "expand_output"
                call["next_output_tokens"] = expanded
                report_progress(self.progress, "输出扩容",
                    f"OS-Copilot {self.purpose} 输出被截断；上限 {context.max_output_tokens}→{expanded}，重新请求完整响应")
            except OperationTimeout as failure:
                call.setdefault("error", failure.to_info().model_dump(mode="json"))
                if timeout_retries >= self.config.max_model_retries:
                    raise
                # Physical attempts, including failures, share the normal call/time/token limits.
                self.check()
                delay = min(self.config.retry_backoff_seconds * 2 ** timeout_retries, 30.0)
                available = self.deadline - monotonic()
                allowance = self.config.max_tokens - self.charged_tokens()
                if self.context.budget is not None:
                    outer = self.context.budget.remaining()
                    available = min(available, outer.elapsed_seconds)
                    allowance = min(allowance, outer.tokens)
                if reservation > allowance:
                    raise BudgetExceeded("OS-Copilot has insufficient token allowance to retry the request", stage="evolution") from failure
                if available <= delay:
                    raise BudgetExceeded("OS-Copilot has no time budget left for model retry", stage="evolution") from failure
                call["retry_delay_seconds"] = delay
                call["recovery"] = "retry_timeout"
                timeout_retries += 1
                report_progress(self.progress, "模型重试",
                    f"OS-Copilot {self.purpose} 请求超时；{delay:g} 秒后重试 {timeout_retries}/{self.config.max_model_retries}")
                sleep(delay)

    def generate(self, request, context, call, reservation):
        started = monotonic()
        try:
            provider = self.provider
            if self.purpose in {"export_metadata", "export_metadata_format"}:
                provider = metadata_provider(provider, self.config.export_response_format)
                call["metadata_response_format"] = getattr(getattr(provider, "config", None), "response_format", None)
            response = provider.generate(request, context)
        except Exception as failure:
            usage = failure.to_info().usage if isinstance(failure, AERError) else None
            if isinstance(failure, OperationTimeout) and (usage is None or usage.total_tokens is None):
                if usage is None:
                    usage = ResourceUsage(input_tokens=None, output_tokens=None, total_tokens=None,
                        model_calls=1, tool_calls=0, elapsed_seconds=monotonic() - started,
                        usage_complete=False, usage_sources=["unknown"])
                usage = usage.replace(pending_reservations=[*usage.pending_reservations,
                    Reservation(operation_id=context.operation_id, stage=context.stage,
                        reserved_tokens=reservation, reserved_tool_calls=0, status="pending")])
                call["reserved_tokens"] = reservation
            if usage is not None:
                self.record.add_usage(usage)
            else:
                self.record.unknown_call(model=True)
            call["error"] = failure.to_info().model_dump(mode="json") if isinstance(failure, AERError) else type(failure).__name__
            raise
        finally:
            call["elapsed_seconds"] = monotonic() - started
        if not isinstance(response, ModelResponse):
            self.record.unknown_call(model=True)
            raise ValueError("OS-Copilot model must return ModelResponse")
        self.record.add_usage(response.usage)
        call["response"] = response.model_dump(mode="json")
        if response.tool_calls or response.usage.tool_calls or not response.usage.model_calls:
            raise ValueError("OS-Copilot requires a complete model-only response")
        if monotonic() - started >= context.model_timeout_seconds:
            raise OperationTimeout("OS-Copilot model call timed out", stage="evolution")
        if response.finish_reason == "length":
            reported = response.usage
            known = reported.input_tokens is not None and reported.output_tokens is not None
            usage = ResourceUsage(input_tokens=reported.input_tokens, output_tokens=reported.output_tokens,
                total_tokens=reported.input_tokens + reported.output_tokens if known else None,
                model_calls=reported.model_calls, tool_calls=reported.tool_calls, elapsed_seconds=reported.elapsed_seconds,
                usage_complete=known and reported.usage_source != "unknown", usage_sources=[reported.usage_source])
            raise ModelOutputTruncated(
                f"OS-Copilot {self.purpose} response truncated at output limit {context.max_output_tokens}",
                stage=context.stage, operation_id=context.operation_id, usage=usage)
        return response.content or ""
