"""AER operator: construct privately, then publish one immutable tool candidate."""

from pathlib import Path
from contextlib import ExitStack
from datetime import datetime, timezone
from time import monotonic

from aer_core.errors import AERError, BudgetExceeded, EvolutionError
from aer_core.progress import report_progress
from aer_core.schemas.evolution import EvolutionCandidate, EvolutionCapabilities, EvolutionResponse
from plugins.agents.execution import SessionRecord
from plugins.agents.tool_selection import CodexToolAdapter
from plugins.tools.generated.bundle import generated_tools
from .engine import ConstructionEngine
from .delivery import verify_delivery
from .model_adapter import ModelAdapter
from .source import load_upstream, UPSTREAM_REVISION
from .recording import ToolConstructionRecord
from .feedback import construction_feedback
from .exporter import export_candidates


class OSCopilotToolEvolver:
    plugin_id = "oscopilot_tool_evolver"

    def __init__(self, config, artifacts, generator, judge, workspaces, progress=None, *, config_root=None):
        self.config, self.artifacts, self.generator, self.judge = config, artifacts, generator, judge
        self.workspaces, self.progress = workspaces, progress
        source = Path(config.source_path).expanduser() if config.source_path else None
        self.source_path = str((config_root / source).resolve() if source and not source.is_absolute() else source) if source else None
        self.adapter = CodexToolAdapter()

    def capabilities(self):
        return EvolutionCapabilities(supported_targets=["tool"], compatible_agents=["codex_cli_agent"],
            required_artifact_kinds=["tool"], supports_multiple_candidates=False, reports_usage=True,
            target_descriptions={"tool": "Use OS-Copilot to generate, execute, judge, repair and store reusable Python tools "
                "in an isolated construction workspace. Exports a frozen tool bundle for Codex MCP; preserves prompt and model."})

    def is_applicable(self, snapshot):
        config = snapshot.model_dump(mode="json")["config"]
        generated = config.get("tooling", {}).get("generated_tools")
        return bool(snapshot.agent_plugin_id == "codex_cli_agent" and generated and generated.get("enabled"))

    def evolve(self, request):
        record, calls, events = SessionRecord(), [], []
        timings = {}
        started_at = datetime.now(timezone.utc)
        candidates, error, status = [], None, "empty"
        try:
            if request.decision.target != "tool" or request.context.stable_snapshot.agent_plugin_id != "codex_cli_agent":
                raise ValueError("OS-Copilot requires a Codex tool evolution request")
            current = self.adapter.read(request.context.stable_snapshot, self.artifacts)
            deadline = monotonic() + self.config.timeout_seconds
            if request.run_context.budget is not None:
                deadline = min(deadline, monotonic() + request.run_context.budget.remaining().elapsed_seconds)
            generator = ModelAdapter(self.generator, request.run_context, self.config, record, calls, deadline,
                purpose="construction", progress=self.progress)
            judge = ModelAdapter(self.judge, request.run_context, self.config, record, calls, deadline,
                purpose="judgment", progress=self.progress)
            upstream = load_upstream(self.source_path)
            prompt_ref = request.context.stable_snapshot.artifacts.get("prompt")
            public_prompt = self.artifacts.get(prompt_ref).decode("utf-8") if prompt_ref else None
            with ExitStack() as resources:
                report_progress(self.progress, "工具工作区", "正在准备独立构造工作区")
                begin = monotonic()
                try:
                    workspace = resources.enter_context(self.workspaces.open(
                        request.context.task, request.context.stable_snapshot, request.run_context))
                finally:
                    timings["workspace_prepare_seconds"] = monotonic() - begin
                generator.check()
                report_progress(self.progress, "工具工作区",
                    f"准备完成，耗时 {timings['workspace_prepare_seconds']:.1f} 秒；本轮剩余 {max(0, deadline - monotonic()):.1f} 秒")
                begin = monotonic()
                try:
                    engine = ConstructionEngine(upstream, self.config, generator, judge, record, calls, events,
                        self.progress, public_prompt=public_prompt)
                    construction = engine.run(request, current, workspace)
                finally:
                    timings["construction_seconds"] = monotonic() - begin
                begin = monotonic()
                try:
                    if construction.tools:
                        report_progress(self.progress, "候选工具转换", f"正在适配 {len(construction.tools)} 个构造产物；验证通过后才会更新生效工具集")
                    prepared = export_candidates(current, construction.tools, workspace.path, engine.generator,
                                                 self.config, events)
                    candidate, examples, interfaces = prepared.bundle, prepared.examples, prepared.interfaces
                    rationale = construction.rationale
                finally:
                    timings["export_seconds"] = monotonic() - begin
            if examples:
                # A fresh task copy has neither construction edits nor its temporary tool library.
                begin = monotonic()
                try:
                    candidate = verify_delivery(current, candidate, examples, self.workspaces, request, record, events, deadline,
                        progress=self.progress, interfaces=interfaces, construction_workspace=workspace.path)
                finally:
                    timings["delivery_seconds"] = monotonic() - begin
            if monotonic() >= deadline:
                raise BudgetExceeded("OS-Copilot construction time budget reached", stage="evolution")
            if candidate != current:
                ref = self.artifacts.put(self.adapter.encode(candidate), "tool")
                changed = sorted(name for name, tool in generated_tools(candidate).items() if generated_tools(current).get(name) != tool)
                candidates = [EvolutionCandidate(candidate_id=request.attempt_id + "/candidate/1",
                    parent_version_id=request.context.stable_snapshot.version_id, target="tool", artifact_updates={"tool": ref},
                    metadata={"operator": self.plugin_id, "rationale": rationale, "round_id": request.round_id,
                              "change_summary": "OS-Copilot tools added/updated: " + ", ".join(changed),
                              "changed_tools": changed, "upstream_revision": UPSTREAM_REVISION,
                              "tool_set_state": "candidate", "activation": "requires_aer_validation_and_commit",
                              "planner": "FridayPlanner"})]
                status = "completed"
        except Exception as failure:
            status = "budget_interrupted" if isinstance(failure, BudgetExceeded) else "error"
            error = failure.to_info() if isinstance(failure, AERError) else EvolutionError(
                "OS-Copilot construction failed: " + str(failure), stage="evolution", plugin_id=self.plugin_id).to_info()
        usage = record.usage()
        if error:
            error = error.replace(stage="evolution", operation_id=request.run_context.operation_id, usage=usage)
        receipt = ToolConstructionRecord(attempt_id=request.attempt_id, task_id=request.context.task.task_id,
            parent_version_id=request.context.stable_snapshot.version_id, upstream_revision=UPSTREAM_REVISION,
            started_at=started_at, finished_at=datetime.now(timezone.utc), config=self.config.model_dump(mode="json"),
            status=status, calls=calls, events=events, timings=timings, usage=usage, error=error)
        ref = self.artifacts.put(receipt.model_dump_json(indent=2).encode("utf-8"), "tool_construction")
        feedback = construction_feedback(events, calls, status, error, ref, self.config.min_generality_score)
        return EvolutionResponse(status=status, candidates=candidates, usage=usage, error=error, record_ref=ref,
                                 construction_feedback=feedback)
