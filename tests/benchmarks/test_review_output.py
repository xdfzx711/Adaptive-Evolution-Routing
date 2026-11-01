import json
import unittest

from plugins.evaluators.review_output import ReviewOutputError, parse_review_output
from plugins.json_output import parse_final


class ReviewOutputTests(unittest.TestCase):
    def test_plain_and_single_fenced_objects_preserve_source_strings(self):
        expected = {"snippet": "  line one\n    line two", "literal": r"keep\n", "code": "```python"}
        body = json.dumps(expected, ensure_ascii=False)
        for wrapped in (body, f"```json\n{body}\n```", f" \r\n```JSON\r\n{body}\r\n```  ", f"```\n{body}\n```"):
            with self.subTest(wrapped=wrapped):
                receipt = {}
                self.assertEqual(parse_review_output(wrapped, receipt), expected)
                self.assertEqual(receipt["output_normalization"]["wrapper"],
                                 "none" if wrapped == body else "single_json_fence")

    def test_ambiguous_or_incomplete_envelopes_are_never_extracted(self):
        invalid = ['Here is JSON:\n```json\n{}\n```', '```json\n{}\n```\nDone.',
                   '```json\n{}\n```\n```json\n{}\n```', '```json\n{}',
                   '```python\n{}\n```', '```json\n{}\n```\n{}', '{} {}']
        for content in invalid:
            with self.subTest(content=content), self.assertRaises(ReviewOutputError):
                parse_review_output(content, {})

    def test_fence_does_not_relax_duplicate_finite_object_or_json_rules(self):
        for body in ('{"x":1,"x":2}', '{"x":NaN}', '{"x":Infinity}', '{"x":1e999}',
                     '[]', 'null', '1', '{"x":1,}', '{"x":"unescaped\nline"}'):
            for content in (body, f"```json\n{body}\n```"):
                with self.subTest(content=content), self.assertRaises(ReviewOutputError):
                    parse_review_output(content, {})

    def test_syntax_error_includes_exact_json_body_location_and_repair_hint(self):
        body = '{\n "verdict":\n}'
        with self.assertRaises(ReviewOutputError) as caught:
            parse_review_output(f"```json\n{body}\n```", {})
        issue = caught.exception.issue
        self.assertEqual(issue["reason"], "json_syntax_error")
        self.assertEqual((issue["line"], issue["column"]), (3, 1))
        self.assertEqual(issue["coordinate_space"], "json_body")
        self.assertEqual(issue["message"], "Expecting value")
        self.assertIn("bare JSON", issue["hint"])

    def test_shared_plugin_parser_stays_strict(self):
        with self.assertRaises(ValueError):
            parse_final('```json\n{}\n```')
