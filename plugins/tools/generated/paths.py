"""Explicit repository path arguments shared by construction, delivery and MCP execution."""

import copy
import ntpath
import posixpath
from pathlib import Path, PureWindowsPath


PATH_MARKER = "x-aer-path"


def _contains_marker(value):
    if isinstance(value, dict):
        return PATH_MARKER in value or any(_contains_marker(v) for v in value.values())
    return isinstance(value, list) and any(_contains_marker(v) for v in value)


def repository_path_fields(schema, prefix=()):
    """Require inline, unambiguous paths so no branch/ref can silently skip binding."""
    if not isinstance(schema, dict):
        return set()
    fields = set()
    if PATH_MARKER in schema:
        if schema[PATH_MARKER] != "repository" or schema.get("type") != "string":
            raise ValueError('x-aer-path must be "repository" on a string schema')
        fields.add(prefix)
    for key, child in schema.get("properties", {}).items():
        fields.update(repository_path_fields(child, prefix + (key,)))
    for keyword in ("items", "additionalProperties"):
        fields.update(repository_path_fields(schema.get(keyword), prefix + ("*",)))
    for key, value in schema.items():
        if key not in {PATH_MARKER, "properties", "items", "additionalProperties"} and _contains_marker(value):
            raise ValueError("Repository path annotations must be inline properties/items/additionalProperties, not " + key)
    return fields


def repository_relative_path(value, workspace, source_workspace=None):
    if not isinstance(value, str) or not value.strip() or "\x00" in value or "\n" in value or "\r" in value:
        raise ValueError("Repository path must be a nonempty path string")
    windows = PureWindowsPath(value)
    absolute = windows.is_absolute() or value.startswith("/")
    if not absolute and (windows.drive or windows.root):
        raise ValueError("Repository path cannot use a drive-relative or rooted Windows path")
    relative = value.replace("\\", "/")
    if absolute:
        relative = None
        path_module = ntpath if windows.drive else posixpath
        for root in (workspace, source_workspace):
            if root is None:
                continue
            root = str(root)
            if bool(PureWindowsPath(root).drive) != bool(windows.drive):
                continue
            normalized, base = path_module.normpath(value), path_module.normpath(root)
            try:
                if path_module.normcase(path_module.commonpath([normalized, base])) != path_module.normcase(base):
                    continue
                relative = path_module.relpath(normalized, base).replace("\\", "/")
                break
            except ValueError:
                continue
        if relative is None:
            raise ValueError("Repository path is outside the current/known construction workspace: " + value)
    relative = posixpath.normpath(relative)
    if relative == ".." or relative.startswith("../"):
        raise ValueError("Repository path escapes its workspace: " + value)
    root = Path(workspace).resolve()
    resolved = (root / relative).resolve()
    if not resolved.is_relative_to(root):
        raise ValueError("Repository path resolves outside its workspace: " + value)
    return relative, str(resolved)


def bind_repository_paths(arguments, schema, workspace, *, source_workspace=None):
    repository_path_fields(schema)
    bindings = []

    def visit(value, shape, pointer):
        if not isinstance(shape, dict):
            return copy.deepcopy(value)
        if shape.get(PATH_MARKER) == "repository":
            relative, resolved = repository_relative_path(value, workspace, source_workspace)
            bindings.append({"pointer": pointer, "original": value, "relative": relative, "resolved": resolved})
            return relative
        if isinstance(value, dict):
            return {key: visit(child, shape.get("properties", {}).get(key, shape.get("additionalProperties")),
                pointer + "/" + key.replace("~", "~0").replace("/", "~1")) for key, child in value.items()}
        if isinstance(value, list):
            return [visit(child, shape.get("items"), pointer + "/" + str(i)) for i, child in enumerate(value)]
        return copy.deepcopy(value)

    return visit(arguments, schema, ""), bindings
