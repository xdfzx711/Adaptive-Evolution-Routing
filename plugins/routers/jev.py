"""TypeSafe Jev Choice router over the public evolution context."""

import json
import os
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from math import isclose, isfinite
from pathlib import Path
from time import monotonic
from urllib.parse import urlsplit

import httpx
from dotenv import dotenv_values
from pydantic import Field, SecretStr, model_validator

from aer_core.config.models import NonBlank, NonNegativeInt, PositiveInt, RoutingSettings, Settings
from aer_core.errors import PluginConfigurationError, RoutingError
from aer_core.schemas.evolution import EvolutionDecision
from aer_core.schemas.routing import JevRoutingRecord
from aer_core.schemas.runtime import RunContext
from aer_core.schemas.usage import ResourceUsage


class JevRouterOptions(Settings):
    dotenv_path: NonBlank | None = None
    api_key_env: NonBlank = "JEV_API_KEY"
    base_url_env: NonBlank = "JEV_BASE_URL"
    model_env: NonBlank = "JEV_MODEL"
    max_trace_events: NonNegativeInt = 8
    max_field_chars: PositiveInt = 1000
    max_state_chars: PositiveInt = 30000


class JevRoutingSettings(RoutingSettings):
    config: JevRouterOptions = Field(default_factory=JevRouterOptions)

    @model_validator(mode="after")
    def jev_selection(self):
        if self.plugin != "jev_router" or self.fixed_target is not None or self.manual_decisions:
            raise ValueError("Jev Router requires plugin=jev_router and no fixed target or manual plan")
        return self


@dataclass(frozen=True)
class JevCredentials:
    api_key: SecretStr
    base_url: str
    model: str

    def __post_init__(self):
        if not self.api_key.get_secret_value().strip() or not self.model.strip():
            raise ValueError("Jev API key and model must be non-empty")
        parts = urlsplit(self.base_url)
        if (parts.scheme not in {"http", "https"} or not parts.hostname or parts.username or parts.password
                or parts.query or parts.fragment):
            raise ValueError("Jev base URL must be HTTP(S), without credentials, query or fragment")

    @property
    def endpoint(self):
        base = self.base_url.rstrip("/")
        if base.endswith("/v1/systemone"):
            return base
        return base + "/systemone" if base.endswith("/v1") else base + "/v1/systemone"


def resolve_jev_credentials(config: JevRouterOptions, config_root: Path) -> JevCredentials:
    path = Path(config.dotenv_path).expanduser() if config.dotenv_path else None
    if path is not None and not path.is_absolute():
        path = (config_root / path).resolve()
    try:
        values = dotenv_values(path, encoding="utf-8-sig", interpolate=False) if path is not None and path.is_file() else {}
        key = os.environ.get(config.api_key_env, values.get(config.api_key_env))
        url = os.environ.get(config.base_url_env, values.get(config.base_url_env))
        model = os.environ.get(config.model_env, values.get(config.model_env))
        missing = [name for name, value in ((config.api_key_env, key), (config.base_url_env, url),
                                          (config.model_env, model)) if not value or not value.strip()]
        if missing:
            raise PluginConfigurationError("Missing Jev environment variables: " + ", ".join(missing),
                stage="setup", plugin_id="jev_router")
        return JevCredentials(SecretStr(key.strip()), url.strip(), model.strip())
    except PluginConfigurationError:
        raise
    except (OSError, UnicodeError, ValueError):
        raise PluginConfigurationError("Cannot load Jev credentials; check dotenv_path, model and base URL",
            stage="setup", plugin_id="jev_router") from None


def _compact(value, max_chars: int):
    if isinstance(value, str):
        return value if len(value) <= max_chars else value[:max_chars] + " [truncated]"
    if isinstance(value, Mapping):
        return {key: _compact(item, max_chars) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_compact(item, max_chars) for item in value[:12]]
    return value


def _usage(data: dict | None, elapsed: float) -> ResourceUsage:
    raw = data.get("usage") if isinstance(data, dict) else None
    raw = raw if isinstance(raw, dict) else {}
    incoming = raw.get("input_tokens")
    outgoing = raw.get("output_tokens")
    incoming = incoming if type(incoming) is int and incoming >= 0 else None
    outgoing = outgoing if type(outgoing) is int and outgoing >= 0 else None
    known = incoming is not None and outgoing is not None
    return ResourceUsage(input_tokens=incoming, output_tokens=outgoing,
        total_tokens=incoming + outgoing if known else None, model_calls=1, tool_calls=0,
        elapsed_seconds=elapsed, usage_complete=known,
        usage_sources=["provider_reported" if known else "unknown"])


class JevRouter:
    plugin_id = "jev_router"

    def __init__(self, config: JevRoutingSettings, artifacts, credentials: JevCredentials,
                 *, transport: httpx.BaseTransport | None = None):
        self.config = config.config
        self.artifacts = artifacts
        self.credentials = credentials
        self.transport = transport

    def _state(self, context):
        limit = self.config.max_field_chars
        history = []
        for item in context.history:
            history.append({"round": item.round_id, "task_id": item.task_id,
                "parent_version": item.stable_version_id, "stable_version_after": item.stable_version_after,
                "attempt_status": item.attempt_status, "error_code": item.error_code,
                "target": item.decision.target if item.decision else None,
                "routing_rationale": item.decision.rationale if item.decision else None,
                "routing_rationale_truncated": item.routing_rationale_truncated,
                "diagnosis": item.diagnosis.reason_code if item.diagnosis else None,
                "candidates": [{"target": candidate.target,
                    "change_summary": candidate.change_summary,
                    "generation_rationale": candidate.generation_rationale,
                    "actual_changes": [edit.model_dump(mode="json") for edit in candidate.actual_changes],
                    "changes_truncated": candidate.changes_truncated,
                    "changes_unavailable": candidate.changes_unavailable,
                    "change_summary_truncated": candidate.change_summary_truncated,
                    "generation_rationale_truncated": candidate.generation_rationale_truncated,
                    "validation": candidate.validation.model_dump(mode="json") if candidate.validation else None,
                    "delta_score": candidate.delta_score,
                    "delta_similar_score": candidate.delta_similar_score,
                    "delta_regression_score": candidate.delta_regression_score,
                    "accepted": candidate.accepted, "selected": candidate.selected, "committed": candidate.committed,
                    "status": candidate.reason, "acceptance_reason": candidate.acceptance_reason}
                    for candidate in item.candidate_summaries]})
        components = {}
        for kind, ref in context.stable_snapshot.artifacts.items():
            try:
                contents = self.artifacts.get(ref).decode("utf-8")
            except UnicodeError:
                contents = "[non-text component]"
            components[kind] = {"excerpt": _compact(contents, limit), "characters": len(contents)}
        trace = context.execution.trajectory[-self.config.max_trace_events:] if self.config.max_trace_events else ()
        return {"task": {"benchmark_id": context.task.benchmark_id,
                    "input": _compact(dict(context.task.input), limit),
                    "metadata": _compact(dict(context.task.public_metadata), limit)},
                "stable_version": context.stable_snapshot.version_id,
                "components": components,
                "execution": {"output": _compact(dict(context.execution.output), limit),
                    "state_summary": _compact(dict(context.execution.state_summary), limit),
                    "recent_trace": [_compact(event.model_dump(mode="json"), limit) for event in trace]},
                "feedback": context.feedback.model_dump(mode="json"),
                "diagnosis": context.diagnosis.model_dump(mode="json", exclude={"usage", "record_ref"})
                    if context.diagnosis else None,
                # ContextBuilder already bounds history; avoid silently truncating actual edits again.
                "history": history,
                "feasible_targets": list(context.feasible_targets)}

    def select(self, context):
        if context.run_context is None:
            raise RoutingError("Jev routing requires a recorded RunContext", stage="routing",
                plugin_id=self.plugin_id, usage=ResourceUsage.zero())
        run_ctx = replace(RunContext.from_record(context.run_context, budget=None), stage="routing",
                          operation_id=context.run_context.operation_id + "/routing")
        options = tuple(context.feasible_targets)
        state = self._state(context)
        request = {"model": self.credentials.model, "state": state,
            "questions": {"evolution_target": {"type": "choice",
                "instructions": "Select the evolution target most likely to improve the next validation result using the public evidence.",
                "criteria": {target: context.target_descriptions.get(target, target) for target in options}}}}
        started_at, started = datetime.now(timezone.utc), monotonic()
        response_data, decision, error = None, None, None
        call_started = False
        usage = ResourceUsage.zero()
        source = "jev" if len(options) > 1 else "single_feasible_target"
        try:
            if not options:
                raise ValueError("Jev requires at least one feasible target")
            if len(options) == 1:
                decision = EvolutionDecision(target=options[0],
                    rationale=f"Only one feasible evolution target: {options[0]}; Jev was not called.")
            else:
                if len(json.dumps(state, ensure_ascii=False)) > self.config.max_state_chars:
                    raise ValueError("Jev state exceeds configured max_state_chars")
                with httpx.Client(transport=self.transport, timeout=run_ctx.model_timeout_seconds,
                                  follow_redirects=False) as client:
                    call_started = True
                    response = client.post(self.credentials.endpoint,
                        headers={"Authorization": "Bearer " + self.credentials.api_key.get_secret_value(),
                                 "Content-Type": "application/json"}, json=request)
                try:
                    response_data = response.json()
                except ValueError:
                    response_data = None
                usage = _usage(response_data, monotonic() - started)
                response.raise_for_status()
                if not isinstance(response_data, dict):
                    raise ValueError("Jev response must be a JSON object")
                answer = response_data.get("answers", {}).get("evolution_target")
                if not isinstance(answer, dict) or answer.get("type") != "choice":
                    raise ValueError("Jev did not return the requested Choice answer")
                chosen, probabilities, confidence = answer.get("choice"), answer.get("probabilities"), answer.get("confidence")
                if chosen not in options or not isinstance(probabilities, dict) or set(probabilities) != set(options):
                    raise ValueError("Jev selected a target outside feasible_targets or returned incomplete probabilities")
                if (any(isinstance(value, bool) or not isinstance(value, (int, float)) or not isfinite(value)
                        or not 0 <= value <= 1 for value in probabilities.values())
                        or not isclose(sum(probabilities.values()), 1.0, abs_tol=0.01)
                        or isinstance(confidence, bool) or not isinstance(confidence, (int, float))
                        or not isfinite(confidence) or not 0 <= confidence <= 1):
                    raise ValueError("Jev returned invalid Choice probabilities or confidence")
                decision = EvolutionDecision(target=chosen,
                    rationale=f"Jev selected {chosen}; probability={probabilities[chosen]:.3f}; "
                              f"confidence={confidence:.3f}. Jev does not provide a textual explanation.")
        except Exception as failure:
            if call_started and usage.model_calls == 0:
                usage = _usage(None, monotonic() - started)
            if isinstance(failure, httpx.TimeoutException):
                detail = "Jev request timed out"
            elif isinstance(failure, httpx.HTTPStatusError):
                detail = f"Jev HTTP {failure.response.status_code}"
            elif isinstance(failure, (ValueError, TypeError, httpx.HTTPError)):
                detail = str(failure) if isinstance(failure, (ValueError, TypeError)) else "Jev network request failed"
            else:
                detail = "Jev routing failed"
            error = RoutingError(detail, stage="routing", plugin_id=self.plugin_id,
                operation_id=run_ctx.operation_id, usage=usage).to_info()
        if decision is not None:
            decision = decision.replace(usage=usage)
        receipt = JevRoutingRecord(run_id=run_ctx.run_id, episode_id=run_ctx.episode_id,
            operation_id=run_ctx.operation_id, source=source, started_at=started_at,
            finished_at=datetime.now(timezone.utc), request=request,
            response=response_data if isinstance(response_data, dict) else None,
            decision=decision, usage=usage, error=error)
        try:
            ref = self.artifacts.put(receipt.model_dump_json(indent=2).encode("utf-8"), "jev_routing_record")
        except Exception as failure:
            raise RoutingError("Could not persist Jev routing input and result", stage="routing",
                plugin_id=self.plugin_id, operation_id=run_ctx.operation_id, usage=usage) from failure
        if error is not None:
            raise RoutingError(error.message, stage="routing", plugin_id=self.plugin_id,
                operation_id=run_ctx.operation_id, usage=usage, evidence_ref=ref)
        return decision.replace(record_ref=ref)
