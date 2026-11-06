"""Bounded native FridayPlanner/FridayExecutor construction with frozen exports."""

from contextlib import redirect_stdout
import copy
import io
import json
import platform
import subprocess
from time import monotonic
from typing import Literal
from pydantic import Field

from aer_core.errors import BudgetExceeded
from aer_core.diagnostic_evidence import evolution_context_payload, FEEDBACK_GUIDANCE
from aer_core.progress import report_progress
from aer_core.schemas.base import DomainModel
from aer_core.schemas.usage import UsageReport
from plugins.tools.generated.bundle import generated_tools
from .repository import ConstructionRepository
from .constructed import ConstructedTool, ConstructionResult
from .native_runtime import NativeEnvironment
from .planning import NativePlanningSession, parse_response_object
from .response_format import NativeResponseAdapter, ResponseFormatError
from .model_adapter import ExportBudgetReserved
from .output_view import execution_view
from .delivery_guidance import DELIVERY_GUIDANCE


class Judgment(DomainModel):
    reasoning: str
    status: Literal["Complete", "Amend", "Replan"]
    score: int = Field(ge=1, le=10)


CONSTRUCTION_GUIDANCE = """
Run in the provided task workspace using native Python code and one <invoke> expression.
Return one closed ```python code block, followed by a standalone <invoke>call(...)</invoke> block.
Keep explanations outside both blocks. Do not include invocation tags inside Python source.
A global workspace string is available to the invocation; use cwd or a workspace call parameter.
Do not install dependencies or run project tests when prohibited by the public task.
Keep examples short and avoid embedding complete predecessor answers in code/invocations.
No tool_spec or MCP publication metadata is required during construction. Publication is a separate step.
Final callable source must be self-contained; temporary helper files will not accompany exports.
""" + DELIVERY_GUIDANCE + FEEDBACK_GUIDANCE


def workspace_changes(path):
    if not (path / ".git").is_dir():
        return None
    result = subprocess.run(["git", "-c", "safe.directory=" + str(path), "-C", str(path),
        "status", "--porcelain", "--untracked-files=all"], capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=10)
    return result.stdout[:16000] if result.returncode == 0 else None


class ConstructionEngine:
    def __init__(self, upstream, config, generator, judge, record, calls, events, progress=None, *, public_prompt=None):
        self.upstream, self.config = upstream, config
        self.generator, self.judge, self.record = generator, judge, record
        self.calls, self.events, self.progress = calls, events, progress
        self.public_prompt = public_prompt

    def run(self, request, current, workspace):
        tools = dict(generated_tools(current))
        selected = set(current.enabled_tools)
        evidence = {"context": evolution_context_payload(request.context),
            "routing_rationale": request.decision.rationale,
            "outer_agent_prompt": self.public_prompt,
            "existing_tools": {name: tool.model_dump(mode="json") for name, tool in tools.items()},
            "outer_agent_capabilities": {"constraints": "Use the public task instructions and outer_agent_prompt; "
                "do not assume shell, file or network operations are permitted. Tools receive caller-supplied "
                "JSON arguments in a fresh task workspace.",
                "selected_tools": sorted(selected), "configured_static_tools": sorted(workspace.tooling.catalog)},
            "construction_limits": {"max_plan_nodes": self.config.max_plan_nodes,
                "max_exported_tools": self.config.max_generated_tools, "supported_node_types": ["Python", "QA"]}}
        self.generator = self.generator.scoped("construction", evidence=evidence)
        self.judge = self.judge.scoped("judgment", evidence=evidence)
        self.generator.allowance.track_pairs = True
        self.judge.allowance = self.generator.allowance
        repository = ConstructionRepository(self.upstream, workspace.repository, tools)
        executor = self.upstream.executor.__new__(self.upstream.executor)
        executor.prompt = copy.deepcopy(self.upstream.prompts["execute_prompt"])
        for key in ("_SYSTEM_PYTHON_SKILL_AND_INVOKE_GENERATE_PROMPT", "_SYSTEM_PYTHON_SKILL_AMEND_AND_INVOKE_PROMPT"):
            executor.prompt[key] += CONSTRUCTION_GUIDANCE
        executor.prompt["_SYSTEM_TASK_JUDGE_PROMPT"] = executor.prompt["_SYSTEM_TASK_JUDGE_PROMPT"].replace(
            "You need to aware that the code I provided does not generate errors, I am just uncertain whether it effectively accomplishes the intended task.",
            "Use the actual Code Error and Code Output; the execution may have failed.") + FEEDBACK_GUIDANCE
        executor.environment = NativeEnvironment(workspace.path, workspace.tooling.tool_timeout_seconds,
            workspace.tooling.generated_tools.python_executable)
        executor.system_version = platform.platform()
        executor.extract_json_from_string = parse_response_object
        responses = NativeResponseAdapter(self.generator, self.config, self.events)
        responses.install(executor)
        executor.llm = self.generator
        planning = NativePlanningSession(self.upstream, self.generator, repository, workspace, self.config, self.events)
        planning.decompose("Develop reusable tools that address the outer agent's observed failure on the original "
            "task in the supplied public development evidence. Investigate the failure and missing capability; "
            "decompose construction into executable Python and analytical QA nodes, with explicit dependencies. "
            "Final deliverables are self-contained callable code executed with representative JSON-compatible "
            "inputs, not builder scripts that merely write another tool file. Specify these same completion "
            "criteria in final node descriptions; constructed helper files will not accompany exports.")
        rationale = next(call["response"]["content"] for call in reversed(self.calls)
                         if call["purpose"] == "planning" and call.get("response"))
        completed, reserve_stop = {}, None
        try:
            self.construct_nodes(request, workspace, executor, planning, repository, responses, completed)
        except ExportBudgetReserved as failure:
            reserve_stop = failure
            self.events.append({"kind": "construction_budget_reserved", "reason": str(failure)})
        self.events.append({"kind": "plan_finished", "completed": all(node.status for node in planning.native.tool_node.values()),
                            "value": planning.snapshot()})
        names = planning.export_names(completed)
        if reserve_stop is not None and not names:
            raise reserve_stop
        return ConstructionResult(tuple(completed[name] for name in names), rationale)

    def construct_nodes(self, request, workspace, executor, planning, repository, responses, completed):
        replans, attempts = 0, 0
        while (goal := planning.next_node()) is not None:
            self.generator.update_export_allowance(completed[name] for name in planning.export_names(completed, record=False))
            self.generator.check()
            attempts += 1
            if attempts > self.config.max_plan_nodes * (self.config.max_replan_iterations + 1):
                raise BudgetExceeded("OS-Copilot construction attempt limit reached", stage="evolution")
            executor.llm = self.generator
            prerequisites = planning.prerequisites(goal.name)
            if goal.node_type == "QA":
                executor.llm = self.generator.scoped("analysis")
                answer = executor.question_and_answer_tool(json.dumps(prerequisites, ensure_ascii=False),
                    "Analyze the original task and observed failure to support reusable tool construction.", goal.description)
                planning.complete(goal.name, answer)
                continue
            report_progress(self.progress, "工具构造", f"OS-Copilot 开始生成 {goal.name}")
            responses.tool = goal.name
            interface = planning.interfaces[goal.name]
            description = goal.description
            if interface is not None:
                description += ("\nIntended public JSON interface (metadata is exported separately, not in Python code):\n"
                    + json.dumps(interface, ensure_ascii=False))
            try:
                code, invoke = executor.generate_tool(goal.name, description, "Python", prerequisites,
                                                      repository.relevant(goal.description))
            except ResponseFormatError:
                self.events.append({"kind": "graph_blocked", "tool": goal.name, "value": planning.snapshot()})
                break
            outcome = "failed"
            for repair in range(self.config.max_repair_iterations + 1):
                self.generator.check()
                response = responses.original
                if request.run_context.budget is not None and not request.run_context.budget.remaining().tool_calls:
                    raise BudgetExceeded("Outer tool-call budget reached", stage="evolution")
                executor.environment.timeout_seconds = min(workspace.tooling.tool_timeout_seconds,
                    max(0.1, self.generator.deadline - monotonic()))
                started = monotonic()
                try:
                    with redirect_stdout(io.StringIO()):
                        state = executor.execute_tool(code, invoke, "Python")
                finally:
                    self.record.add_usage(UsageReport(input_tokens=0, output_tokens=0, model_calls=0, tool_calls=1,
                        elapsed_seconds=monotonic() - started, usage_source="provider_reported"))
                result = executor.environment.last_result
                execution_event_index = len(self.events)
                self.events.append({"kind": "execution", "tool": goal.name, "repair": repair,
                    "source": code, "result": result, "upstream_example_invoke": invoke,
                    "working_tree_changes": workspace_changes(workspace.path)})
                executor.llm = self.judge
                try:
                    with redirect_stdout(io.StringIO()):
                        reasoning, status, score = executor.judge_tool(code,
                            json.dumps({"description": description, "predecessors": prerequisites}, ensure_ascii=False),
                            state, goal.next_action)
                finally:
                    self.generator.allowance.judgment_tokens = 0
                judgment = Judgment(reasoning=reasoning, status=status, score=score)
                self.events.append({"kind": "judgment", "tool": goal.name, "repair": repair,
                                    "score_scope": "construction_library",
                                    **judgment.model_dump(mode="json")})
                if judgment.status == "Complete" and state.error is None:
                    if judgment.score >= self.config.min_generality_score:
                        repository.store_source(goal.name, code, goal.description)
                        self.events.append({"kind": "stored", "tool": goal.name, "repair": repair,
                            "score": judgment.score, "scope": "construction", "deliverable": planning.deliverables[goal.name]})
                        report_progress(self.progress, "临时工具入库", f"{goal.name}；通用性评分 {judgment.score}/10")
                    # Native library admission is independent of AER candidate eligibility.
                    completed[goal.name] = ConstructedTool(goal.name, code, invoke, goal.description,
                                                          response, interface, result)
                    planning.complete(goal.name, json.dumps(execution_view(result, limit=12000,
                        full_output_ref={"record": "construction", "event_index": execution_event_index, "field": "result"}),
                        ensure_ascii=False), {goal.name: code})
                    outcome = "complete"
                    break
                if judgment.status == "Replan":
                    if replans < self.config.max_replan_iterations:
                        replans += 1
                        planning.replan(goal.name, json.dumps({"critique": reasoning, "error": state.error,
                            "output": state.result, "predecessors": prerequisites}, ensure_ascii=False))
                        outcome = "replanned"
                    else:
                        self.events.append({"kind": "replan_limit", "tool": goal.name})
                    break
                if repair == self.config.max_repair_iterations:
                    self.events.append({"kind": "repair_limit", "tool": goal.name})
                    break
                executor.llm = self.generator.scoped("repair")
                try:
                    code, invoke = executor.repair_tool(code, description, "Python", state, reasoning, prerequisites)
                except ResponseFormatError:
                    break
                report_progress(self.progress, "工具修复", f"{goal.name}；第 {repair + 1} 次修复")
            if outcome == "failed":
                self.events.append({"kind": "graph_blocked", "tool": goal.name, "value": planning.snapshot()})
                break
