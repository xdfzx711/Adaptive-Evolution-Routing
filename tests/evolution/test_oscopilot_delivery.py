"""Publication acceptance, including real Git checks against disposable pristine source."""

from contextlib import contextmanager
from pathlib import Path
from time import monotonic
import unittest
from unittest.mock import patch

from aer_core.errors import BudgetExceeded
from plugins.agents.execution import SessionRecord
from plugins.evolution.oscopilot.delivery import verify_delivery, patch_text
from plugins.evolution.oscopilot.interfaces import JSONToolInterface
from plugins.tools.generated.bundle import ToolBundle
from tests.evolution.test_oscopilot import Workspaces, request_fixture
from tests.tools.test_generated import tool


PATCH = "--- a/sample.py\n+++ b/sample.py\n@@ -1 +1 @@\n-value = 1\n+value = 2\n"


def patch_interface():
    return {"input_schema": {"type": "object"}, "output_schema": {"type": "object"},
        "acceptance": {"kind": "patch", "patch_pointer": "/patch_text", "success_schema": {
            "type": "object", "required": ["success", "patch_text", "error_message"],
            "properties": {"success": {"const": True}, "patch_text": {"type": "string", "minLength": 1},
                           "error_message": {"type": "null"}}}}}


class PristineWorkspaces(Workspaces):
    def __init__(self):
        super().__init__()
        self.paths = []
        self.contents_at_close = []

    @contextmanager
    def open(self, *args):
        with super().open(*args) as workspace:
            (workspace.path / "sample.py").write_text("value = 1\n", encoding="utf-8")
            self.paths.append(str(workspace.path))
            yield workspace
            self.contents_at_close.append((workspace.path / "sample.py").read_text(encoding="utf-8"))


class DeliveryTests(unittest.TestCase):
    def deliver(self, value, interface, *, mutate=False, current=None, deadline=None, arguments=None,
                input_schema=None, construction_workspace=None):
        _, request = request_fixture()
        current = current or ToolBundle()
        implementation = tool()
        if input_schema is not None:
            implementation = implementation.replace(input_schema=input_schema)
        proposed = ToolBundle(enabled_tools=["source_summary"], generated_tools={"source_summary": implementation})
        record, events, workspaces = SessionRecord(), [], PristineWorkspaces()
        def execute(implementation, arguments, workspace, **kwargs):
            if mutate:
                Path(workspace, "sample.py").write_text("value = 2\n", encoding="utf-8")
            return {"status": "completed", "error": None, "result": value, "stdout": "", "elapsed_seconds": 0.001}
        with patch("plugins.evolution.oscopilot.delivery.execute_generated", side_effect=execute) as worker:
            result = verify_delivery(current, proposed, {"source_summary": arguments or {"path": "sample.py"}}, workspaces, request, record,
                events, deadline if deadline is not None else monotonic() + 30,
                interfaces={"source_summary": interface} if interface is not None else {},
                construction_workspace=construction_workspace)
        return result, events[0], record, workspaces, worker

    def test_delivery_rebinds_known_old_absolute_path_before_worker_execution(self):
        from tests.evolution.test_oscopilot import node
        import copy
        interface = copy.deepcopy(node("Inspect", deliverable=True)["interface"])
        interface["input_schema"]["properties"]["path"]["x-aer-path"] = "repository"
        result, event, _, _, worker = self.deliver({"exists": True}, interface,
            arguments={"path": "/tmp/removed/task/sample.py"}, input_schema=interface["input_schema"],
            construction_workspace="/tmp/removed/task")
        self.assertTrue(event["passed"])
        self.assertEqual(worker.call_args.args[1], {"path": "sample.py"})
        self.assertEqual(event["original_arguments"], {"path": "/tmp/removed/task/sample.py"})
        self.assertEqual(Path(event["path_bindings"][0]["resolved"]), Path(event["workspace"], "sample.py"))
        self.assertIn("source_summary", result.generated_tools)

    def test_caught_file_error_is_not_success_and_preserves_stable_tool(self):
        stable = ToolBundle(enabled_tools=["source_summary"], generated_tools={
            "source_summary": tool("def source_summary(path, workspace):\n    return {'stable': True}")})
        failed = {"success": False, "patch_text": "", "error_message": "[Errno 2] No such file: old construction path"}
        result, event, record, workspaces, _ = self.deliver(failed, patch_interface(), current=stable)
        self.assertEqual(result, stable)
        self.assertFalse(event["passed"])
        self.assertEqual(event["result"]["result"], failed)
        self.assertEqual(event["result"]["status"], "completed")
        self.assertIsNone(event["result"]["error"])
        self.assertIn("business success", event["acceptance_error"])
        self.assertEqual(len(workspaces.paths), 1)
        self.assertEqual(record.usage().tool_calls, 1)

    def test_claimed_success_cannot_publish_empty_malformed_or_unapplicable_patch(self):
        for text in ("", "   \n", "not a diff", PATCH.replace("value = 1", "value = 99")):
            with self.subTest(text=text):
                result, event, _, _, _ = self.deliver(
                    {"success": True, "patch_text": text, "error_message": None}, patch_interface())
                self.assertEqual(result, ToolBundle())
                self.assertFalse(event["passed"])
                self.assertIn("acceptance_error", event)

    def test_valid_patch_is_checked_without_applying_on_another_pristine_copy(self):
        value = {"success": True, "patch_text": PATCH, "error_message": None}
        result, event, record, workspaces, _ = self.deliver(value, patch_interface(), mutate=True)
        self.assertEqual(set(result.generated_tools), {"source_summary"})
        self.assertTrue(event["passed"])
        self.assertTrue(event["patch_check"]["passed"])
        self.assertNotEqual(event["workspace"], event["patch_check"]["workspace"])
        self.assertEqual(workspaces.contents_at_close, ["value = 2\n", "value = 1\n"])
        self.assertEqual(record.usage().tool_calls, 2)

    def test_json_diagnostic_can_legitimately_return_false_and_empty_findings(self):
        interface = {"input_schema": {"type": "object"}, "output_schema": {"type": "object"},
            "acceptance": {"kind": "json", "success_schema": {"type": "object",
                "required": ["has_errors", "findings"],
                "properties": {"has_errors": {"const": False}, "findings": {"const": []}}}}}
        result, event, record, workspaces, _ = self.deliver({"has_errors": False, "findings": []}, interface)
        self.assertTrue(event["passed"])
        self.assertEqual(set(result.generated_tools), {"source_summary"})
        self.assertEqual(len(workspaces.paths), 1)
        self.assertEqual(record.usage().tool_calls, 1)

    def test_missing_contract_never_falls_back_to_execution_only(self):
        old_interface = {"input_schema": {"type": "object"}, "output_schema": {"type": "object"}}
        for interface in (None, old_interface):
            result, event, _, workspaces, worker = self.deliver({"success": False}, interface)
            self.assertEqual(result, ToolBundle())
            self.assertFalse(event["passed"])
            self.assertIn("acceptance contract", event["acceptance_error"])
            self.assertFalse(workspaces.paths)
            worker.assert_not_called()

    def test_bad_or_type_only_contract_is_rejected_during_planning_validation(self):
        variants = []
        for pointer in (None, "patch_text", "/bad~2escape"):
            interface = patch_interface()
            interface["acceptance"]["patch_pointer"] = pointer
            variants.append(interface)
        for schema in ({}, {"type": "object"}, {"type": "QuerySet"}):
            interface = patch_interface()
            interface["acceptance"]["success_schema"] = schema
            variants.append(interface)
        for interface in variants:
            with self.subTest(interface=interface), self.assertRaises(ValueError):
                JSONToolInterface.model_validate(interface)

    def test_patch_pointer_handles_root_arrays_escaping_and_missing_values(self):
        self.assertEqual(patch_text(PATCH, ""), PATCH)
        self.assertEqual(patch_text({"a/b~": [PATCH]}, "/a~1b~0/0"), PATCH)
        for value, pointer in (({}, "/patch"), ([PATCH], "/-1"), ([PATCH], "/00"), ({"patch": None}, "/patch")):
            with self.subTest(pointer=pointer), self.assertRaises(ValueError):
                patch_text(value, pointer)

    def test_patch_check_respects_deadline_and_records_timeout_as_failed_acceptance(self):
        with self.assertRaises(BudgetExceeded):
            self.deliver({}, patch_interface(), deadline=monotonic() - 1)
        import subprocess
        with patch("plugins.evolution.oscopilot.delivery.subprocess.run",
                   side_effect=subprocess.TimeoutExpired("git apply", 1)):
            result, event, record, _, _ = self.deliver(
                {"success": True, "patch_text": PATCH, "error_message": None}, patch_interface())
        self.assertEqual(result, ToolBundle())
        self.assertFalse(event["patch_check"]["passed"])
        self.assertIn("error", event["patch_check"])
        self.assertEqual(record.usage().tool_calls, 2)
