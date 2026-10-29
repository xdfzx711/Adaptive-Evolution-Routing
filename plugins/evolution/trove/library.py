"""Self-contained, versioned Python toolboxes stored as ordinary tool artifacts."""

import ast
import keyword
import re
from typing import Literal

import jsonschema
from pydantic import Field, field_validator, model_validator

from aer_core.schemas.base import DomainModel, Items, JsonObject
from plugins.tool_selection import ToolDescriptor, ToolSelection


SCHEMA_VERSION = "aer.trove.toolbox.v1"
RESERVED_NAMES = {"solve", "task", "__builtins__"}
MODULE_EXPORTS = {
    "math": "ceil floor sqrt isqrt factorial gcd lcm comb perm prod fsum log log2 log10 exp sin cos tan pi e inf isclose",
    "re": "search match fullmatch findall finditer split sub subn escape compile IGNORECASE MULTILINE DOTALL",
    "json": "loads dumps",
    "statistics": "mean median mode multimode pstdev pvariance stdev variance",
    "collections": "Counter defaultdict deque",
    "itertools": "chain combinations combinations_with_replacement permutations product accumulate groupby islice zip_longest",
    "functools": "reduce lru_cache cache partial",
    "operator": "itemgetter add sub mul truediv floordiv mod neg eq lt le gt ge",
    "ast": "parse walk iter_child_nodes get_source_segment dump literal_eval FunctionDef AsyncFunctionDef ClassDef Import ImportFrom Call Name Attribute Constant Assign Return",
}
BLOCKED_ATTRIBUTES = {"gi_frame", "cr_frame", "ag_frame", "f_back", "f_globals", "f_locals", "tb_frame"}


def validate_source(source: str, name: str) -> None:
    """Check the supported Python subset. This is not an OS security sandbox."""
    if len(source) > 24000:
        raise ValueError("Tool source exceeds 24000 characters")
    tree = ast.parse(source)
    if len(tree.body) != 1 or not isinstance(tree.body[0], ast.FunctionDef) or tree.body[0].name != name:
        raise ValueError("Source must define exactly one function named " + name)
    function = tree.body[0]
    for node in ast.walk(tree):
        if isinstance(node, (ast.ClassDef, ast.AsyncFunctionDef, ast.Global, ast.Nonlocal)):
            raise ValueError("Unsupported Python statement")
        if isinstance(node, ast.FunctionDef):
            if node.decorator_list:
                raise ValueError("Decorators are not supported")
            for default in (*node.args.defaults, *[d for d in node.args.kw_defaults if d is not None]):
                ast.literal_eval(default)
        if isinstance(node, ast.Attribute) and (node.attr.startswith("_") or node.attr in BLOCKED_ATTRIBUTES):
            raise ValueError("Private and frame attributes are not supported")
        if isinstance(node, ast.Name) and node.id.startswith("__"):
            raise ValueError("Private runtime names are not supported")
        if isinstance(node, ast.Import):
            if any(alias.name not in MODULE_EXPORTS for alias in node.names):
                raise ValueError("Import is outside the supported standard library")
        if isinstance(node, ast.ImportFrom):
            if node.level or node.module not in MODULE_EXPORTS or any(
                alias.name not in MODULE_EXPORTS[node.module].split() for alias in node.names
            ):
                raise ValueError("Import is outside the supported standard library")
    if function.args.posonlyargs:
        raise ValueError("Tool inputs must accept keyword arguments")


class ToolExample(DomainModel):
    arguments: JsonObject
    expected: JsonObject


class PythonTool(DomainModel):
    name: str
    description: str = Field(min_length=1)
    input_schema: JsonObject
    source: str = Field(min_length=1)
    dependencies: Items[str] = ()
    examples: Items[ToolExample] = Field(min_length=1, max_length=12)

    @model_validator(mode="after")
    def valid_tool(self):
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,63}", self.name) or keyword.iskeyword(self.name) or self.name in RESERVED_NAMES:
            raise ValueError("Generated tool requires a public Python function name")
        schema = self.model_dump(mode="json")["input_schema"]
        pending = [schema]
        while pending:
            value = pending.pop()
            if isinstance(value, dict):
                if any(key in value and (not isinstance(value[key], str) or not value[key].startswith("#"))
                       for key in ("$ref", "$dynamicRef")):
                    raise ValueError("Tool schemas may only reference local definitions")
                pending.extend(value.values())
            elif isinstance(value, list):
                pending.extend(value)
        jsonschema.Draft202012Validator.check_schema(schema)
        if schema.get("type") != "object":
            raise ValueError("Tool input_schema must describe an object")
        if len(set(self.dependencies)) != len(self.dependencies) or self.name in self.dependencies:
            raise ValueError("Tool dependencies must be unique and exclude itself")
        validate_source(self.source, self.name)
        for example in self.examples:
            jsonschema.validate(example.model_dump(mode="json")["arguments"], schema)
        return self

    def descriptor(self):
        return ToolDescriptor(description=self.description, version="trove-python-v1", input_schema=self.input_schema)


class ToolLibrary(ToolSelection):
    schema_version: Literal["aer.trove.toolbox.v1"] = SCHEMA_VERSION
    tools: Items[PythonTool] = Field(default=(), max_length=128)
    usage_counts: JsonObject = Field(default_factory=dict)

    @field_validator("tools")
    @classmethod
    def canonical_order(cls, tools):
        return tuple(sorted(tools, key=lambda tool: tool.name))

    @model_validator(mode="after")
    def valid_library(self):
        names = [tool.name for tool in self.tools]
        if len(set(names)) != len(names):
            raise ValueError("Tool library names must be unique")
        known = set(names)
        for tool in self.tools:
            if not set(tool.dependencies) <= known:
                raise ValueError("Missing generated tool dependency")
            references = {node.id for node in ast.walk(ast.parse(tool.source)) if isinstance(node, ast.Name)
                and isinstance(node.ctx, ast.Load)} & (known - {tool.name})
            if not references <= set(tool.dependencies):
                raise ValueError("Calls to generated tools require declared dependencies")
        if any(name not in known or type(count) is not int or count < 0 for name, count in self.usage_counts.items()):
            raise ValueError("Usage counts require known tools and non-negative integers")
        self.ordered_tools()
        return self

    def ordered_tools(self):
        remaining = {tool.name: tool for tool in self.tools}
        ordered, seen = [], set()
        while remaining:
            ready = sorted(name for name, tool in remaining.items() if set(tool.dependencies) <= seen)
            if not ready:
                raise ValueError("Generated tool dependencies contain a cycle")
            for name in ready:
                ordered.append(remaining.pop(name))
                seen.add(name)
        return tuple(ordered)

    def validate_catalog(self, catalog):
        generated = {tool.name for tool in self.tools}
        if generated & set(catalog):
            raise ValueError("Generated tool shadows a configured tool")
        if not set(self.enabled_tools) <= generated | set(catalog):
            raise ValueError("Selected tool is absent from the toolbox and configured catalog")

    def functional_value(self):
        return self.model_dump(mode="json", exclude={"usage_counts"})


def read_library(content: bytes) -> ToolLibrary:
    import json
    value = json.loads(content)
    if "schema_version" in value:
        return ToolLibrary.model_validate(value)
    selection = ToolSelection.model_validate(value)
    return ToolLibrary(enabled_tools=selection.enabled_tools)


def trim_library(library: ToolLibrary, protected: set[str], limit: int) -> ToolLibrary:
    """Drop the least used removable tools while preserving dependency closure."""
    tools = {tool.name: tool for tool in library.tools}
    while len(tools) > limit:
        required = {name for tool in tools.values() for name in tool.dependencies}
        removable = set(tools) - required - protected
        if not removable:
            raise ValueError("Toolbox limit cannot preserve required tools")
        name = min(removable, key=lambda item: (library.usage_counts.get(item, 0), item))
        del tools[name]
    removed = {tool.name for tool in library.tools} - tools.keys()
    return library.replace(tools=tuple(tools[name] for name in sorted(tools)),
        enabled_tools=tuple(name for name in library.enabled_tools if name not in removed),
        usage_counts={name: count for name, count in library.usage_counts.items() if name in tools})
