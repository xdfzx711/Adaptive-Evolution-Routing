import copy
from pathlib import Path
import tempfile
import unittest

from plugins.tools.generated.paths import bind_repository_paths, repository_path_fields
from plugins.evolution.oscopilot.portability import assert_bound_examples, assert_portable


PATH = {"type": "string", "x-aer-path": "repository"}
SCHEMA = {"type": "object", "properties": {"path": PATH}}


class RepositoryPathTests(unittest.TestCase):
    def test_known_windows_and_posix_roots_become_portable_relative_paths(self):
        with tempfile.TemporaryDirectory() as root:
            for old, value in ((r"C:\Temp\old\task", "c:/TEMP/old/task/django/db/models/deletion.py"),
                               ("/tmp/old/task", "/tmp/old/task/django/db/models/deletion.py")):
                bound, events = bind_repository_paths({"path": value}, SCHEMA, root, source_workspace=old)
                self.assertEqual(bound, {"path": "django/db/models/deletion.py"})
                self.assertEqual(events[0]["original"], value)
                self.assertEqual(Path(events[0]["resolved"]), Path(root, "django/db/models/deletion.py"))

    def test_nested_lists_and_maps_bind_without_rewriting_text_or_mutating_input(self):
        schema = {"type": "object", "properties": {
            "files": {"type": "array", "items": {"type": "object", "properties": {"path": PATH}}},
            "outputs": {"type": "object", "additionalProperties": PATH},
            "source": {"type": "string"}, "patch": {"type": "string"}}}
        with tempfile.TemporaryDirectory() as root:
            old = "/tmp/old/task"
            arguments = {"files": [{"path": old + "/sample.py"}], "outputs": {"report": "reports\\result.json"},
                "source": "open('/tmp/old/task/sample.py')", "patch": "--- /tmp/old/task/sample.py\n+++ other"}
            original = copy.deepcopy(arguments)
            bound, events = bind_repository_paths(arguments, schema, root, source_workspace=old)
            self.assertEqual(arguments, original)
            self.assertEqual(bound["files"], [{"path": "sample.py"}])
            self.assertEqual(bound["outputs"], {"report": "reports/result.json"})
            self.assertEqual(bound["source"], original["source"])
            self.assertEqual(bound["patch"], original["patch"])
            self.assertEqual([e["pointer"] for e in events], ["/files/0/path", "/outputs/report"])

    def test_sibling_roots_escapes_drive_relative_and_unknown_absolute_paths_are_rejected(self):
        with tempfile.TemporaryDirectory() as root:
            for value in ("/tmp/old/task-other/a.py", "/tmp/old/task/../outside.py", "../outside.py",
                          "C:relative.py", "\\rooted.py", "C:/unknown/a.py", "", "a\nb.py"):
                with self.subTest(value=value), self.assertRaises(ValueError):
                    bind_repository_paths({"path": value}, SCHEMA, root, source_workspace="/tmp/old/task")

    def test_current_absolute_and_relative_paths_resolve_in_each_new_workspace(self):
        with tempfile.TemporaryDirectory() as first, tempfile.TemporaryDirectory() as second:
            arguments, _ = bind_repository_paths({"path": str(Path(first, "sample.py"))}, SCHEMA, first)
            rebound, bindings = bind_repository_paths(arguments, SCHEMA, second)
            self.assertEqual(rebound, {"path": "sample.py"})
            self.assertEqual(Path(bindings[0]["resolved"]), Path(second, "sample.py"))

    def test_path_annotations_cannot_hide_under_schema_branches_or_refs(self):
        for schema in ({"type": "number", "x-aer-path": "repository"},
                       {"type": "string", "x-aer-path": "absolute"},
                       {"anyOf": [SCHEMA]}, {"$defs": {"file": PATH}, "$ref": "#/$defs/file"}):
            with self.subTest(schema=schema), self.assertRaises(ValueError):
                repository_path_fields(schema)

    def test_literal_defaults_joined_paths_and_constructor_metadata_require_native_repair(self):
        root = "C:/Temp/aer-tool-construction-old/task"
        sources = [f"def tool(path={root!r}): return path",
                   "def tool(): return Path('C:/Temp', 'aer-tool-construction-old', 'task')",
                   "def tool(): return 'C:/Temp/' + 'aer-tool-' + 'construction-old/task'",
                   "def tool(): return f'C:/Temp/aer-tool-construction-old/task'",
                   "def tool(): return 'C:\\\\Temp\\\\aer-tool-construction-old\\\\task'"]
        for source in sources:
            with self.subTest(source=source), self.assertRaisesRegex(ValueError, "Non-portable"):
                assert_portable(source, {}, root)
        with self.assertRaisesRegex(ValueError, "Non-portable"):
            assert_portable("def tool(): return 1", {"constructor_arguments": {"root": root}}, root)
        assert_portable("def tool(path, workspace): return str(Path(workspace) / path)", {}, root)

    def test_unmarked_absolute_example_requires_annotation_without_changing_evidence(self):
        with tempfile.TemporaryDirectory() as root:
            with self.assertRaisesRegex(ValueError, "x-aer-path"):
                assert_bound_examples({"path": str(Path(root, "sample.py"))}, root)
            evidence = {"source": "open('/tmp/aer-tool-construction-old/task/file.py')", "note": "Evidence retained"}
            assert_bound_examples(evidence, root)
