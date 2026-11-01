import copy
import json
import unittest

from plugins.evolution.oscopilot.output_view import execution_view


class OutputViewTests(unittest.TestCase):
    def test_duplicate_returns_are_removed_without_losing_logs_or_mutating_receipt(self):
        value = {"source": "line\n" * 40000}
        receipt = {"status": "completed", "result": value, "error": None,
                   "stdout": "diagnostic\n" + str(value) + "\n<return>\n" + str(value) + "\n</return>\n"}
        original = copy.deepcopy(receipt)
        reference = {"record": "construction", "event_index": 3, "field": "result"}
        view = execution_view(receipt, limit=12000, full_output_ref=reference)
        self.assertEqual(receipt, original)
        self.assertEqual(view['stdout'], 'diagnostic')
        self.assertTrue(view['result']['truncated'])
        self.assertEqual(view['result']['keys'], ['source'])
        self.assertEqual(view['full_output_ref'], reference)
        self.assertLess(len(json.dumps(view)), 15000)

    def test_scalar_duplicate_does_not_remove_digits_from_diagnostics(self):
        view = execution_view({'result': 1, 'stdout': 'line 123\n1\n<return>\n1\n</return>\n'},
                              limit=4000, full_output_ref={})
        self.assertEqual(view['result'], 1)
        self.assertEqual(view['stdout'], 'line 123')

    def test_stdout_only_and_large_error_are_retained_as_marked_previews(self):
        view = execution_view({'result': None, 'stdout': 'log' * 100000, 'error': 'error' * 10000},
                              limit=4000, full_output_ref={})
        self.assertTrue(view['stdout']['truncated'])
        self.assertTrue(view['error']['truncated'])
        self.assertLess(len(json.dumps(view)), 4000)

    def test_small_json_results_keep_their_types_and_values(self):
        for value in ({'ok': False}, [], 0, False, 'text', None):
            with self.subTest(value=value):
                view = execution_view({'result': value, 'stdout': ''}, limit=4000, full_output_ref={})
                self.assertEqual(view['result'], value)
                self.assertNotIn('stdout', view)
