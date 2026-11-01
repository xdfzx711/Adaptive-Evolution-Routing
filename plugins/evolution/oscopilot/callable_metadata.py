"""Static export facts, without executing or transforming native tool source."""

import ast


class MetadataError(ValueError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def _signature(node, *, method=False):
    args = node.args
    positional = [*args.posonlyargs, *args.args]
    required = positional[:len(positional) - len(args.defaults)]
    named = [*positional, *args.kwonlyargs]
    required += [arg for arg, default in zip(args.kwonlyargs, args.kw_defaults) if default is None]
    static = any(isinstance(d, ast.Name) and d.id == "staticmethod" for d in node.decorator_list)
    implicit = positional[0].arg if method and not static and positional else None
    return {
        "parameters": [a.arg for a in named if a.arg != implicit],
        "required": [a.arg for a in required if a.arg not in {implicit, "workspace"}],
        "positional_only": [a.arg for a in args.posonlyargs if a.arg != implicit],
        "accepts_extra_keywords": args.kwarg is not None,
    }


def _returns_callable(node):
    # Only definite factories: every return in this function returns a locally
    # defined function or lambda. Mixed/indirect behavior stays with delivery execution.
    nested = {n.name for n in node.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    returns = []

    def visit(item):
        if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
            return
        if isinstance(item, ast.Return):
            returns.append(item.value)
        for child in ast.iter_child_nodes(item):
            visit(child)

    for statement in node.body:
        visit(statement)
    # Assignments can shadow a local function name; leave those cases to runtime.
    assigned = {n.id for statement in node.body for n in ast.walk(statement)
                if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store)}
    nested -= assigned
    return bool(returns) and all(isinstance(value, ast.Lambda) or
        isinstance(value, ast.Name) and value.id in nested for value in returns)


def callable_inventory(source):
    facts = []
    for node in ast.parse(source).body:
        if isinstance(node, ast.FunctionDef):
            facts.append({"entrypoint": node.name, "kind": "function", **_signature(node),
                          "returns_callable": _returns_callable(node)})
        elif isinstance(node, ast.ClassDef):
            call = next((n for n in node.body if isinstance(n, ast.FunctionDef) and n.name == "__call__"), None)
            init = next((n for n in node.body if isinstance(n, ast.FunctionDef) and n.name == "__init__"), None)
            # Inherited/dynamic signatures are deliberately left to the existing runtime.
            facts.append({"entrypoint": node.name, "kind": "class",
                          "signature": _signature(call, method=True) if call else None,
                          "constructor": _signature(init, method=True) if init else None})
    return facts


def _validate_arguments(signature, schema, arguments):
    if signature is None:
        return
    declared = set(schema.get("properties", {})) | set(schema.get("required", [])) | set(arguments)
    unknown = declared - set(signature["parameters"])
    if unknown and not signature["accepts_extra_keywords"]:
        raise MetadataError("metadata_signature_mismatch", f"Unknown callable parameters: {sorted(unknown)}")
    missing = set(signature["required"]) - set(schema.get("required", []))
    if missing:
        raise MetadataError("metadata_signature_mismatch", f"Schema must require callable parameters: {sorted(missing)}")
    if signature["positional_only"]:
        raise MetadataError("incompatible_callable", "JSON runtime passes keyword arguments; positional-only parameters need a native callable wrapper")


def validate_callable(metadata, inventory):
    spec = metadata.specification.model_dump(mode="json")
    fact = next((f for f in inventory if f["entrypoint"] == spec["entrypoint"] and f["kind"] == spec["kind"]), None)
    if fact is None:
        raise MetadataError("metadata_entrypoint_mismatch", "Entrypoint must be a synchronous top-level callable from callable_inventory; nested functions are not runtime entrypoints")
    if fact.get("returns_callable"):
        raise MetadataError("incompatible_callable", "Builder returns a function object instead of a JSON result. Native construction must provide a directly callable top-level tool; export cannot lift nested code or invent arguments")
    signature = fact if fact["kind"] == "function" else fact["signature"]
    _validate_arguments(signature, spec["input_schema"], spec["example_arguments"])
    _validate_arguments(signature, metadata.interface.model_dump(mode="json")["input_schema"], spec["example_arguments"])
    if fact["kind"] == "class" and fact["constructor"] is not None:
        args = spec["constructor_arguments"]
        _validate_arguments(fact["constructor"], {"properties": dict.fromkeys(args), "required": list(args)}, args)


def ensure_exportable(inventory):
    if inventory and all(f.get("returns_callable") for f in inventory):
        raise MetadataError("incompatible_callable", "Only builder functions returning callables were produced. Native construction must expose a top-level tool taking JSON arguments and returning JSON; metadata conversion cannot repair this implementation")
    if not inventory:
        raise MetadataError("incompatible_callable", "Source has no synchronous top-level callable to export")
