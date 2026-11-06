"""Boundary validation and dependency injection for the upstream FridayPlanner."""

from contextlib import redirect_stdout
from aer_core.diagnostic_evidence import FEEDBACK_GUIDANCE
import copy
import io
import json
import platform
import re
from types import SimpleNamespace
from typing import Literal

from aer_core.schemas.base import DomainModel, Identifier, Items
from plugins.json_output import parse_final
from plugins.tools.generated.bundle import valid_name
from .delivery_guidance import DELIVERY_GUIDANCE


PLANNING_CONTRACT = """
AER construction environment and export intent:
Use the native node-keyed graph with description, dependencies and type (Python or QA).
Optionally declare deliverable (boolean): true for final reusable Python tools, false for internal helpers
and validation-only scripts. A tool may be deliverable even when a later node tests it.
Missing export hints never block native construction; completed unmarked tools may also be converted.
An optional interface may describe JSON inputs/outputs; publication metadata is completed AFTER native construction.
Do not put publication-format instructions into task descriptions. Internal helpers need no tool_spec,
MCP schema or acceptance metadata. Execute them using their native code and invocation.
Final tools will be called by Codex with JSON arguments in a fresh task workspace. Use caller-supplied
text and structured data, or paths when the public task supplies files and permits their use.
Task input in public evidence is available to the model; it is not automatically a file or Python global.
Plan final nodes as directly invocable, self-contained tools. Writing another tool file alone does not
exercise that tool and does not package it for export; plan an actual callable and representative execution.
Internal helper nodes may create files. A final node's description and completion criteria must instead
require the callable's behavior, not writing its definition to a .py file. Include any constructed helper
logic in the exported source; do not make final tools import temporary modules from construction paths.
When file access is appropriate, use workspace-relative paths, cwd or a workspace call parameter;
avoid temporary roots in source/defaults. Each invocation has a fresh Python namespace, with a global
workspace string. Do not assume a repository, application or database exists. Do not install packages,
access external sources or execute project tests when forbidden by the task.
Read the full task, trajectory, results, diagnosis and construction_feedback in the public evidence.
Diagnosis hypotheses and model_assessment remain hypotheses, not confirmed defects. Investigate them.
Historical failures and paths are evidence, not current bindings. Preserve the original repair objective.
AER converts selected completed tools into a candidate bundle; only outer validation can make it active.
""" + DELIVERY_GUIDANCE + FEEDBACK_GUIDANCE


class NativePlanFields(DomainModel):
    description: Identifier
    dependencies: Items[Identifier]
    type: Literal["Python", "QA"]


def parse_response_object(content):
    """Native prompts allow an explanation followed by one fenced JSON object."""
    blocks = re.findall(r"```(?:json)?\s*\n?(.*?)```", content, re.S)
    if len(blocks) > 1:
        raise ValueError("Expected one JSON response block")
    return parse_final(blocks[0].strip() if blocks else content)


def dependency_order(graph):
    """Validate a whole graph before native methods mutate it."""
    ordered, visiting, visited = [], set(), set()

    def visit(name):
        if name not in graph:
            raise ValueError("Unknown plan dependency: " + name)
        if name in visiting:
            raise ValueError("Cycle in FridayPlanner task graph: " + name)
        if name in visited:
            return
        visiting.add(name)
        for dependency in graph[name]:
            visit(dependency)
        visiting.remove(name)
        visited.add(name)
        ordered.append(name)

    for name in graph:
        visit(name)
    return ordered


class NativePlanningSession:
    def __init__(self, upstream, model, repository, workspace, config, events):
        self.config, self.events, self.model = config, events, model
        self.repository, self.current = repository, None
        self.deliverables = {}
        self.interfaces = {}
        self.forbidden_names = set(workspace.tooling.catalog)
        self.native = upstream.planner.__new__(upstream.planner)
        self.native.reset_plan()
        self.native.prompt = copy.deepcopy(upstream.prompts["planning_prompt"])
        for key in ("_SYSTEM_TASK_DECOMPOSE_PROMPT", "_SYSTEM_TASK_REPLAN_PROMPT"):
            self.native.prompt[key] += PLANNING_CONTRACT
        self.native.llm = model.scoped("planning")
        self.native.tool_manager = repository.native
        self.native.system_version = platform.platform()
        self.native.environment = SimpleNamespace(working_dir=str(workspace.path),
            list_working_dir=lambda: "\n".join(sorted(path.name for path in workspace.path.iterdir())))
        self.native.extract_json_from_string = self.parse_graph

    def parse_graph(self, response):
        raw = parse_response_object(response)
        if not isinstance(raw, dict):
            raise ValueError("FridayPlanner must return a node-keyed JSON object")
        if self.current is not None and not raw:
            raise ValueError("Native replanning must insert at least one prerequisite")
        if len(raw) + len(self.native.tool_node) > self.config.max_plan_nodes:
            raise ValueError("FridayPlanner graph exceeds max_plan_nodes")
        graph = {name: list(dependencies) for name, dependencies in self.native.tool_graph.items()}
        nodes, hints, interfaces = {}, {}, {}
        for name, value in raw.items():
            valid_name(name)
            if name in self.native.tool_node or name in self.forbidden_names:
                raise ValueError("Plan node collides with an existing node or static tool: " + name)
            if not isinstance(value, dict):
                raise ValueError("Each plan node must be a JSON object")
            base = NativePlanFields.model_validate({key: value[key] for key in
                ("description", "dependencies", "type") if key in value})
            graph[name] = list(base.dependencies)
            nodes[name] = base.model_dump(mode="json")
            hint = value.get("deliverable")
            hints[name] = hint if type(hint) is bool else None
            if base.type == "QA":
                hints[name] = False
            interfaces[name] = value.get("interface") if isinstance(value.get("interface"), dict) else None
            ignored = [key for key, normalized in (("deliverable", hints[name]), ("interface", interfaces[name]))
                       if key in value and value[key] != normalized]
            if ignored:
                self.events.append({"kind": "publication_hint_ignored", "tool": name, "fields": ignored})
        order = dependency_order(graph)
        # Upstream creates nodes in insertion order and dereferences predecessors immediately.
        nodes = {name: nodes[name] for name in order if name in nodes}
        if self.current is not None:
            graph[self.current].append(next(reversed(nodes)))
            dependency_order(graph)
        self.deliverables.update(hints)
        self.interfaces.update(interfaces)
        # Keep AER publication metadata outside native ActionNode/optimization state.
        return nodes

    def export_names(self, completed, *, record=True):
        """Offer completed products for conversion, without inferring roles from graph edges."""
        explicit = [name for name in completed if self.deliverables[name] is True]
        unmarked = [name for name in reversed(completed) if self.deliverables[name] is None]
        selected = explicit + unmarked
        if record:
            self.events.append({"kind": "export_selected", "tools": selected,
                                "explicit": explicit, "unmarked": unmarked})
        return selected

    def snapshot(self):
        return {"execution_order": list(self.native.sub_task_list), "nodes": {
            name: {"description": node.description, "type": node.node_type,
                   "deliverable": self.deliverables[name],
                   "interface": self.interfaces[name],
                   "dependencies": list(self.native.tool_graph[name]), "completed": node.status,
                   "return_val": node.return_val, "next_action": dict(node.next_action)}
            for name, node in self.native.tool_node.items()}}

    def decompose(self, task):
        self.native.llm = self.model.scoped("planning")
        with redirect_stdout(io.StringIO()):
            self.native.decompose_task(task, self.repository.descriptions(task))
        self.events.append({"kind": "plan", "planner": "FridayPlanner", "value": self.snapshot()})

    def replan(self, name, reasoning):
        self.current = name
        self.native.llm = self.model.scoped("replanning")
        before = set(self.native.tool_node)
        try:
            with redirect_stdout(io.StringIO()):
                # Upstream's declared signature takes reasoning before current_task.
                self.native.replan_task(reasoning, name, self.repository.descriptions(reasoning))
        finally:
            self.current = None
        # Native add_new_tool adds the edge but omits its reverse next_action metadata.
        for dependency in self.native.tool_graph[name]:
            if dependency not in before:
                self.native.tool_node[dependency].next_action[name] = self.native.tool_node[name].description
        self.events.append({"kind": "replan", "planner": "FridayPlanner", "tool": name,
                            "reasoning": reasoning, "value": self.snapshot()})

    def prerequisites(self, name):
        return json.loads(self.native.get_pre_tasks_info(name))

    def complete(self, name, result, code=None):
        node = self.native.tool_node[name]
        self.native.update_tool(name, return_val=result, relevant_code=code, status=True, node_type=node.node_type)
        self.events.append({"kind": "node_completed", "tool": name, "type": node.node_type,
                            "return_val": result, "next_action": dict(node.next_action)})

    def next_node(self):
        with redirect_stdout(io.StringIO()):
            error = self.native.topological_sort()
        if error:
            raise ValueError(error)
        if not self.native.sub_task_list:
            return None
        name = self.native.sub_task_list[0]
        if any(not self.native.tool_node[dependency].status for dependency in self.native.tool_graph[name]):
            raise ValueError("FridayPlanner scheduled a node before its prerequisites")
        return self.native.tool_node[name]
