"""AER publication check after the native construction workspace has been released."""

from time import monotonic
import subprocess
import jsonschema

from aer_core.errors import BudgetExceeded
from aer_core.progress import report_progress
from aer_core.schemas.usage import UsageReport
from plugins.tools.generated.bundle import ToolBundle, generated_tools
from plugins.tools.generated.runtime import execute_generated
from plugins.tools.generated.paths import bind_repository_paths, repository_path_fields
from .interfaces import JSONToolInterface
from .portability import assert_bound_examples, assert_portable


def remaining_time(request, deadline):
    remaining = deadline - monotonic()
    if request.run_context.budget is not None:
        budget = request.run_context.budget.remaining()
        if not budget.tool_calls:
            raise BudgetExceeded("Outer tool delivery budget reached", stage="evolution")
        remaining = min(remaining, budget.elapsed_seconds)
    if remaining <= 0:
        raise BudgetExceeded("AER tool delivery time budget reached", stage="evolution")
    return remaining


def patch_text(value, pointer):
    """Select an artifact using an explicitly declared RFC 6901 JSON Pointer."""
    for raw in pointer.split("/")[1:]:
        key = raw.replace("~1", "/").replace("~0", "~")
        if isinstance(value, dict) and key in value:
            value = value[key]
        elif isinstance(value, list) and key.isascii() and key.isdecimal() and str(int(key)) == key and int(key) < len(value):
            value = value[int(key)]
        else:
            raise ValueError("Declared patch_pointer does not select a result value")
    if not isinstance(value, str) or not value.strip():
        raise ValueError("Delivered patch must be a nonempty string")
    return value


def check_patch(text, workspaces, request, record, deadline, progress):
    # The tool may have edited its execution workspace. Validate against another pristine copy.
    remaining_time(request, deadline)
    report_progress(progress, "工具复验", "正在准备干净源码并检查补丁可应用性")
    with workspaces.open(request.context.task, request.context.stable_snapshot, request.run_context) as workspace:
        timeout = min(workspace.tooling.tool_timeout_seconds, remaining_time(request, deadline))
        started = monotonic()
        check = {"workspace": str(workspace.path), "passed": False,
                 "command": ["git", "apply", "--check", "--no-index", "--whitespace=nowarn", "-p1", "-"]}
        try:
            process = subprocess.run(check["command"], input=text, cwd=workspace.path,
                capture_output=True, encoding="utf-8", errors="replace", timeout=timeout)
            check.update(returncode=process.returncode, stdout=process.stdout, stderr=process.stderr,
                         passed=process.returncode == 0)
        except (OSError, subprocess.TimeoutExpired) as failure:
            check["error"] = str(failure)
        finally:
            record.add_usage(UsageReport(input_tokens=0, output_tokens=0, model_calls=0, tool_calls=1,
                elapsed_seconds=monotonic() - started, usage_source="provider_reported"))
    return check


def verify_delivery(current, proposed, examples, workspaces, request, record, events, deadline, *, progress=None, interfaces=None,
                    construction_workspace=None):
    tools, selected = dict(generated_tools(current)), set(current.enabled_tools)
    verified = []
    for name, arguments in examples.items():
        remaining_time(request, deadline)
        event = {"kind": "delivery_check", "tool": name, "passed": False, "arguments": arguments}
        events.append(event)
        try:
            interface = JSONToolInterface.model_validate((interfaces or {}).get(name)).model_dump(mode="json")
        except ValueError as failure:
            event["acceptance_error"] = "Missing or invalid AER delivery acceptance contract: " + str(failure)
            continue
        event["acceptance"] = interface["acceptance"]
        tool = generated_tools(proposed)[name]
        tool_data = tool.model_dump(mode="json")
        try:
            if construction_workspace is not None:
                assert_portable(tool.source, {key: value for key, value in tool_data.items() if key != "source"}, construction_workspace)
            if not repository_path_fields(interface["input_schema"]) <= repository_path_fields(tool_data["input_schema"]):
                raise ValueError("Tool must preserve all planned repository path annotations")
        except ValueError as failure:
            event["path_error"] = str(failure)
            continue
        report_progress(progress, "工具复验", f"正在准备 {name} 的独立交付复验工作区")
        with workspaces.open(request.context.task, request.context.stable_snapshot, request.run_context) as workspace:
            event["workspace"] = str(workspace.path)
            try:
                bound_arguments, bindings = bind_repository_paths(arguments, tool_data["input_schema"], workspace.path,
                    source_workspace=construction_workspace)
                assert_bound_examples(bound_arguments, workspace.path, construction_workspace)
                jsonschema.validate(bound_arguments, tool_data["input_schema"])
                jsonschema.validate(bound_arguments, interface["input_schema"])
            except (ValueError, jsonschema.ValidationError) as failure:
                event["path_error"] = str(failure)
                continue
            event.update(original_arguments=arguments, arguments=bound_arguments, path_bindings=bindings)
            remaining = remaining_time(request, deadline)
            started = monotonic()
            try:
                result = execute_generated(tool, bound_arguments, workspace.path,
                    timeout_seconds=min(workspace.tooling.tool_timeout_seconds, remaining),
                    python_executable=workspace.tooling.generated_tools.python_executable)
            finally:
                record.add_usage(UsageReport(input_tokens=0, output_tokens=0, model_calls=0, tool_calls=1,
                    elapsed_seconds=monotonic() - started, usage_source="provider_reported"))
        event.update(workspace=str(workspace.path), result=result)
        passed = result["status"] == "completed" and result["error"] is None
        if passed:
            try:
                jsonschema.validate(result["result"], interface["output_schema"])
            except jsonschema.ValidationError as failure:
                passed = False
                event["interface_error"] = "AER public JSON output interface: " + failure.message
        if passed:
            try:
                jsonschema.validate(result["result"], interface["acceptance"]["success_schema"])
            except jsonschema.ValidationError as failure:
                passed = False
                event["acceptance_error"] = "AER delivery business success: " + failure.message
        if passed and interface["acceptance"]["kind"] == "patch":
            try:
                text = patch_text(result["result"], interface["acceptance"]["patch_pointer"])
            except ValueError as failure:
                passed = False
                event["acceptance_error"] = str(failure)
            else:
                event["patch_check"] = check_patch(text, workspaces, request, record, deadline, progress)
                passed = event["patch_check"]["passed"]
                if not passed:
                    event["acceptance_error"] = "Delivered patch failed git apply --check against pristine source"
        event["passed"] = passed
        if passed:
            tools[name] = tool
            selected.add(name)
            verified.append(name)
    events.append({"kind": "export_verified", "tools": verified})
    return ToolBundle(enabled_tools=sorted(selected), generated_tools=tools) if verified else current
