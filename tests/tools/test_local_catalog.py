from pathlib import Path
import tempfile
import unittest

from plugins.agents.codex import CodexConfig, controlled_config
from plugins.tool_selection import ToolSelection
from plugins.tools.catalog import ToolingConfig
from plugins.tools.local import local_tool_registry
from tests.tools.helpers import LOCAL_CATALOG


class LocalToolsTests(unittest.TestCase):
    def test_structure_and_syntax_reflect_file_changes_without_executing_project_code(self):
        registry = local_tool_registry()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "module.py"
            source.write_text("import os\nclass Demo:\n def run(self): return 1\nraise RuntimeError('never execute')\n", encoding="utf-8")
            result = registry.get("python_structure").invoke(root, {"path": "module.py"})
            self.assertEqual([item["name"] for item in result["definitions"]], ["Demo", "Demo.run"])
            self.assertEqual(result["imports"], [{"text": "import os", "line": 1}])
            self.assertTrue(registry.get("python_syntax_check").invoke(root, {"path": "module.py"})["valid"])
            source.write_text("def broken(\n", encoding="utf-8")
            checked = registry.get("python_syntax_check").invoke(root, {"path": "module.py"})
            self.assertFalse(checked["valid"])
            self.assertEqual(checked["line"], 1)
            self.assertEqual(source.read_text(encoding="utf-8"), "def broken(\n")
            source.write_bytes(b"# coding: latin-1\nname = 'caf\xe9'\n")
            self.assertTrue(registry.get("python_syntax_check").invoke(root, {"path": "module.py"})["valid"])

    def test_workspace_paths_and_local_plugin_registration_are_enforced(self):
        registry = local_tool_registry()
        with self.assertRaises(ValueError):
            registry.register(registry.get("python_structure"))
        with self.assertRaises(ValueError):
            registry.get("unknown")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "task"
            root.mkdir()
            outside = root.parent / "private.py"
            outside.write_text("answer = 42", encoding="utf-8")
            for arguments in ({"path": "../private.py"}, {"path": str(outside)}, {"path": "file.txt"},
                              {"path": "missing.py"}, {"path": "file.py", "code": "print(1)"}):
                with self.subTest(arguments=arguments), self.assertRaises(ValueError):
                    registry.get("python_structure").invoke(root, arguments)
            try:
                (root / "linked.py").symlink_to(outside)
            except OSError:
                pass  # The native Windows account may lack symlink privileges.
            else:
                with self.assertRaises(ValueError):
                    registry.get("python_structure").invoke(root, {"path": "linked.py"})

    def test_tooling_is_opt_in_and_selections_are_order_independent(self):
        legacy = controlled_config(CodexConfig())
        self.assertNotIn("tooling", legacy)
        self.assertNotIn("tool_catalog", legacy)
        settings = CodexConfig(tooling={"catalog": LOCAL_CATALOG}, initial_tools=["python_syntax_check"])
        self.assertNotIn("initial_tools", controlled_config(settings))
        self.assertEqual(set(controlled_config(settings)["tool_catalog"]), set(LOCAL_CATALOG))
        self.assertEqual(ToolSelection(enabled_tools=["b", "a"]), ToolSelection(enabled_tools=["a", "b"]))
        with self.assertRaises(ValueError):
            ToolSelection(enabled_tools=["a", "a"])

    def test_invalid_connections_versions_and_initial_tools_fail_during_config_validation(self):
        cases = [
            {"catalog": {"bad name": {"kind": "local", "plugin": "python_structure"}}},
            {"catalog": {"a": {"kind": "local", "plugin": "missing"}}},
            {"catalog": {"a": {"kind": "local", "plugin": "python_structure", "version": "wrong"}}},
            {"catalog": {"a": {"kind": "mcp", "server": "missing", "tool_name": "x", "description": "x", "version": "1"}}},
            {"catalog": {}, "servers": {"x": {"transport": "stdio", "version": "1", "command": []}}},
            {"catalog": {}, "servers": {"x": {"transport": "streamable_http", "version": "1", "url": "https://user:secret@host/mcp"}}},
            {"catalog": {}, "servers": {"x": {"transport": "streamable_http", "version": "1", "url": "https://host/mcp?token=secret"}}},
        ]
        for values in cases:
            with self.subTest(values=values), self.assertRaises(ValueError):
                ToolingConfig.model_validate(values)
        for changes in ({"initial_tools": []}, {"tooling": {"catalog": LOCAL_CATALOG}, "initial_tools": ["missing"]},
                        {"tooling": {"catalog": LOCAL_CATALOG}, "initial_tools": ["python_structure", "python_structure"]}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                CodexConfig(**changes)
