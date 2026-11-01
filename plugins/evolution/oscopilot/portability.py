"""Reject construction-directory dependencies before calling the native repair loop."""

import ast
import re
from pathlib import PureWindowsPath

from plugins.tools.generated.paths import repository_relative_path


def assert_bound_examples(arguments, workspace, source_workspace=None):
    """Unmarked complete repository paths require metadata repair, never guessed substitution."""
    if isinstance(arguments, dict):
        for value in arguments.values():
            assert_bound_examples(value, workspace, source_workspace)
    elif isinstance(arguments, list):
        for value in arguments:
            assert_bound_examples(value, workspace, source_workspace)
    elif isinstance(arguments, str) and "\n" not in arguments and "\r" not in arguments:
        if not (arguments.startswith("/") or PureWindowsPath(arguments).is_absolute()):
            return
        try:
            repository_relative_path(arguments, workspace, source_workspace)
        except ValueError:
            if "aer-tool-construction-" not in arguments.casefold():
                return
        raise ValueError('Unbound construction path in example_arguments; mark this path input with '
            '"x-aer-path":"repository" and use a repository-relative example')


def assert_portable(source, metadata, workspace):
    roots = [str(workspace).replace("\\", "/").rstrip("/").casefold()]

    def check(value):
        normalized = value.replace("\\", "/").casefold()
        if any(re.search(re.escape(root) + r"(?=$|[/\s'\"])", normalized) for root in roots) or re.search(
                r"aer-tool-construction-[a-z0-9_-]+", normalized):
            raise ValueError("Non-portable construction path in generated source/metadata. Use repository-relative "
                "paths and the injected workspace call parameter (or current cwd); do not capture temporary roots.")

    def constant(node):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value
        if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Div)):
            left, right = constant(node.left), constant(node.right)
            if left is not None and right is not None:
                return left + ("/" if isinstance(node.op, ast.Div) else "") + right
        if isinstance(node, ast.JoinedStr):
            values = [constant(child) for child in node.values]
            if all(value is not None for value in values):
                return "".join(values)
        if isinstance(node, ast.Call):
            name = node.func.id if isinstance(node.func, ast.Name) else getattr(node.func, "attr", None)
            if name in {"Path", "PurePath", "PureWindowsPath", "PurePosixPath", "join"}:
                values = [constant(arg) for arg in node.args]
                if values and all(value is not None for value in values):
                    return "/".join(values)
        return None

    for node in ast.walk(ast.parse(source)):
        value = constant(node)
        if value is not None:
            check(value)

    def visit(value):
        if isinstance(value, str):
            check(value)
        elif isinstance(value, dict):
            for child in value.values():
                visit(child)
        elif isinstance(value, (list, tuple)):
            for child in value:
                visit(child)
    visit(metadata)
