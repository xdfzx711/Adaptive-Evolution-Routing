"""Small Python analysis tools; read only files within the task workspace."""

import ast
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
import tokenize

from plugins.tool_selection import ToolDescriptor


FILE_SCHEMA = {"type": "object", "properties": {"path": {"type": "string",
    "description": "Path of one Python source file relative to the current task workspace."}},
    "required": ["path"], "additionalProperties": False}


def read_source(workspace, arguments):
    if set(arguments) != {"path"} or not isinstance(arguments["path"], str):
        raise ValueError("Exactly one string path is required")
    root = Path(workspace).resolve()
    supplied = Path(arguments["path"])
    source = (root / supplied).resolve()
    if supplied.is_absolute() or supplied.drive or not source.is_relative_to(root) or source.suffix != ".py":
        raise ValueError("path must name a Python file inside the current workspace")
    if not source.is_file() or source.stat().st_size > 1024 * 1024:
        raise ValueError("Source file is missing or exceeds the 1 MiB analysis limit")
    with tokenize.open(source) as stream:
        return source.relative_to(root).as_posix(), stream.read()


def syntax_check(workspace, arguments):
    name, text = read_source(workspace, arguments)
    try:
        ast.parse(text, filename=name)
    except SyntaxError as error:
        return {"path": name, "valid": False, "line": error.lineno, "column": error.offset,
                "message": error.msg}
    return {"path": name, "valid": True}


def structure(workspace, arguments):
    name, text = read_source(workspace, arguments)
    tree = ast.parse(text, filename=name)
    definitions, imports = [], []

    def visit(body, prefix=""):
        for node in body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                qualified = prefix + node.name
                definitions.append({"name": qualified, "kind": "class" if isinstance(node, ast.ClassDef) else "function",
                    "line": node.lineno, "end_line": node.end_lineno})
                visit(node.body, qualified + ".")
            elif isinstance(node, (ast.Import, ast.ImportFrom)):
                imports.append({"text": ast.get_source_segment(text, node), "line": node.lineno})
            else:
                # Include definitions/imports nested in conditionals, try blocks and loops.
                for field in ("body", "orelse", "finalbody"):
                    visit(getattr(node, field, ()), prefix)
                for handler in getattr(node, "handlers", ()):
                    visit(handler.body, prefix)

    visit(tree.body)
    return {"path": name, "definitions": definitions[:200], "imports": imports[:100],
            "truncated": len(definitions) > 200 or len(imports) > 100}


@dataclass(frozen=True)
class LocalTool:
    plugin_id: str
    descriptor: ToolDescriptor
    invoke: Callable


class LocalToolRegistry:
    def __init__(self):
        self._tools = {}

    def register(self, tool: LocalTool):
        if tool.plugin_id in self._tools:
            raise ValueError("Local tool plugin is already registered: " + tool.plugin_id)
        self._tools[tool.plugin_id] = tool

    def get(self, plugin_id):
        if plugin_id not in self._tools:
            raise ValueError("Unknown local tool plugin: " + plugin_id)
        return self._tools[plugin_id]


def local_tool_registry():
    registry = LocalToolRegistry()
    registry.register(LocalTool("python_structure", ToolDescriptor(version="1.0.0", input_schema=FILE_SCHEMA,
        description="Return Python definitions, methods, imports and source line ranges without executing project code."), structure))
    registry.register(LocalTool("python_syntax_check", ToolDescriptor(version="1.0.0", input_schema=FILE_SCHEMA,
        description="Check one Python file for syntax errors without importing it, executing tests or installing project dependencies."), syntax_check))
    return registry
