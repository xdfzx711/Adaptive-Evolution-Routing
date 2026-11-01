"""Replay real malformed repair replies without executing their repository-writing code."""

import ast
from pathlib import Path
import unittest

from plugins.evolution.oscopilot.config import OSCopilotConfig
from plugins.evolution.oscopilot.response_format import NativeResponseAdapter, ResponseFormatError
from tests.evolution import test_oscopilot_model
from tests.execution.helpers import RecordingModel, response


class NoRecovery:
    def scoped(self, *args, **kwargs):
        raise AssertionError("Unambiguous extraction needs no model request")


def reply(code="def inspect():\n    return 1", invocation="inspect()"):
    return f"```python\n{code}\n```\n<invoke>{invocation}</invoke>"


class ResponseFormatTests(unittest.TestCase):
    def adapter(self, model=None, **settings):
        return NativeResponseAdapter(model or NoRecovery(), OSCopilotConfig(**settings), [])

    def test_real_repairs_parse_without_code_changes_or_model_calls(self):
        root = Path(__file__).parents[1] / "fixtures/oscopilot"
        for number in (1, 2):
            with self.subTest(repair=number):
                original = (root / f"repair_format_{number}.txt").read_text(encoding="utf-8")
                adapter = self.adapter()
                parsed = adapter.resolve(original)
                ast.parse(parsed.code)
                self.assertIn(parsed.code, original)
                self.assertEqual(parsed.invoke, "analyze_annotation_dependencies(query=None, annotation_aliases=set())")
                self.assertNotIn("<invoke>", parsed.code)
                self.assertIs(adapter.resolve(original), parsed)

    def test_prose_and_string_tag_mentions_do_not_override_actual_invocation(self):
        source = 'def inspect():\n    return """\n<invoke>wrong()</invoke>\n"""'
        content = 'Explanation: `<invoke>bad()</invoke>` is an example.\n' + reply(source)
        parsed = self.adapter().resolve(content)
        self.assertEqual(parsed.code, source)
        self.assertEqual(parsed.invoke, 'inspect()')

    def test_unclosed_fence_keeps_multiline_string_tags_in_source(self):
        source = 'def inspect():\n    return """\n<invoke>wrong()</invoke>\n"""'
        parsed = self.adapter().resolve('```python\n' + source + '\n<invoke>inspect()</invoke>')
        self.assertEqual(parsed.code, source)
        self.assertEqual(parsed.invoke, 'inspect()')

    def test_actual_python_syntax_error_remains_for_native_repair(self):
        source = 'def inspect():\n    return ('
        parsed = self.adapter().resolve(reply(source))
        self.assertEqual(parsed.code, source)
        with self.assertRaises(SyntaxError):
            ast.parse(parsed.code)

    def test_invocation_examples_in_non_python_fences_are_not_public_calls(self):
        content = '```text\n<invoke>example()</invoke>\n```\n' + reply()
        self.assertEqual(self.adapter().resolve(content).invoke, 'inspect()')

    def test_unclosed_source_string_is_not_silently_trimmed_at_a_tag(self):
        content = '```python\ndef inspect():\n    return """\n<invoke>inspect()</invoke>'
        with self.assertRaises(ResponseFormatError):
            self.adapter().resolve(content)

    def test_multiline_class_call_is_supported(self):
        invocation = 'Inspector(\n    "source"\n)()'
        self.assertEqual(self.adapter().resolve(reply(invocation=invocation)).invoke, invocation)

    def test_missing_or_non_call_invocation_is_reported_without_inventing_code(self):
        for content in ('```python\ndef inspect(): return 1\n```', reply(invocation='1 + 2')):
            adapter = self.adapter()
            with self.subTest(content=content), self.assertRaises(ResponseFormatError):
                adapter.resolve(content)
            self.assertEqual(adapter.events[-1]['kind'], 'response_format_failed')

    def test_ambiguous_invocations_are_not_silently_selected_when_recovery_disabled(self):
        adapter = self.adapter(max_response_format_repairs=0)
        with self.assertRaises(ResponseFormatError):
            adapter.resolve(reply() + '\n<invoke>another()</invoke>')

    def test_format_recovery_selects_only_original_content_without_full_evidence(self):
        provider = RecordingModel(lambda *_: response('{"code_candidate": 0, "invoke_candidate": 1}'))
        model = test_oscopilot_model.OSCopilotModelTests().adapter(provider).scoped('construction', evidence={'secret_marker': 'FULL_TASK_EVIDENCE'})
        adapter = self.adapter(model)
        original = reply() + '\n<invoke>inspect(2)</invoke>'
        parsed = adapter.resolve(original)
        self.assertEqual(parsed.code, 'def inspect():\n    return 1')
        self.assertEqual(parsed.invoke, 'inspect(2)')
        self.assertEqual(len(provider.requests), 1)
        self.assertNotIn('FULL_TASK_EVIDENCE', provider.requests[0][0].model_dump_json())
        self.assertEqual(model.calls[0]['purpose'], 'response_format')
        self.assertIs(adapter.resolve(original), parsed)

    def test_invalid_selection_exhausts_format_budget_and_never_accepts_new_source(self):
        for answer in ('not JSON', '{"code_candidate": 3, "invoke_candidate": 0}',
                       '{"code_candidate": true, "invoke_candidate": 0}', '{"source": "invented"}'):
            provider = RecordingModel(lambda *_: response(answer))
            adapter = self.adapter(test_oscopilot_model.OSCopilotModelTests().adapter(provider))
            with self.subTest(answer=answer), self.assertRaises(ResponseFormatError):
                adapter.resolve(reply() + '\n<invoke>another()</invoke>')
            self.assertEqual(len(provider.requests), 1)
