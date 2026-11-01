"""Transport recovery stays outside the native tool generation/repair methods."""

from time import monotonic
import unittest
import json
from unittest.mock import patch

from aer_core.errors import BudgetExceeded, ExecutionError, OperationTimeout
from aer_core.schemas.usage import ResourceUsage
from plugins.agents.execution import SessionRecord
from plugins.evolution.oscopilot.config import OSCopilotConfig
from plugins.evolution.oscopilot.model_adapter import ModelAdapter, ModelOutputTruncated, ExportBudgetReserved
from tests.execution.helpers import RecordingModel, context, response


MESSAGES = [{"role": "user", "content": "Inspect the source and generate a reusable tool."}]


def timeout(ctx, *, known=False):
    usage = (ResourceUsage(input_tokens=3, output_tokens=2, total_tokens=5, model_calls=1,
        tool_calls=0, elapsed_seconds=60, usage_complete=True, usage_sources=["simulated"]) if known else
        ResourceUsage(input_tokens=None, output_tokens=None, total_tokens=None, model_calls=1,
            tool_calls=0, elapsed_seconds=60, usage_complete=False, usage_sources=["unknown"]))
    return OperationTimeout("Qwen HTTP request timed out", stage=ctx.stage,
        operation_id=ctx.operation_id, plugin_id="qwen_model", usage=usage)


class OSCopilotModelTests(unittest.TestCase):
    def test_factory_only_product_does_not_reserve_metadata_tokens(self):
        from plugins.evolution.oscopilot.constructed import ConstructedTool
        product = ConstructedTool('builder', 'def builder():\n    def analyzer(): return []\n    return analyzer',
                                  'builder()', 'Builder', '', None, {})
        adapter = self.adapter(RecordingModel(lambda *_: response('ok')), export_token_reserve=49000)
        adapter.update_export_allowance([product])
        self.assertEqual(adapter.allowance.exportable_tools, 0)
        self.assertEqual(adapter.allowance.export_tokens, 0)

    def test_metadata_format_recovery_obeys_shared_call_budget(self):
        from plugins.evolution.oscopilot.constructed import ConstructedTool
        from plugins.evolution.oscopilot.exporter import export_metadata
        product = ConstructedTool('tool', 'def tool(): return []', 'tool()', 'Tool', '', None, {})
        provider = RecordingModel(lambda *_: response('{'))
        adapter = self.adapter(provider, max_model_calls=1)
        with self.assertRaisesRegex(BudgetExceeded, 'model-call budget'):
            export_metadata(product, adapter, adapter.config, [])
        self.assertEqual(len(provider.requests), 1)
        self.assertEqual(adapter.record.usage().model_calls, 1)

    def test_large_export_output_uses_same_bounded_messages_for_reserve_and_request(self):
        from plugins.evolution.oscopilot.constructed import ConstructedTool
        from plugins.evolution.oscopilot.exporter import export_metadata, specification
        from tests.evolution.test_oscopilot import implementation, node
        metadata = {'specification': specification(implementation()).model_dump(mode='json'),
                    'interface': node('Inspect', deliverable=True)['interface']}
        provider = RecordingModel(lambda *_: response(json.dumps(metadata)))
        adapter = self.adapter(provider, export_token_reserve=49000)
        adapter.evidence = 'FULL_DEVELOPMENT_EVIDENCE' * 20000
        value = {'full_content': 'source code\n' * 20000}
        execution = {'status': 'completed', 'result': value, 'error': None,
                     'stdout': str(value) + '\n<return>\n' + str(value) + '\n</return>\n'}
        product = ConstructedTool('source_summary', 'def source_summary(path, workspace): return {}',
                                 "source_summary('sample.py', workspace=workspace)", 'Inspect', implementation(), None, execution)
        with patch.object(adapter, 'estimate_input_tokens', wraps=adapter.estimate_input_tokens) as estimate:
            adapter.update_export_allowance([product])
            reserved_messages = estimate.call_args.args[0]
        reserved_input = adapter.estimate_input_tokens(reserved_messages)
        self.assertEqual(adapter.allowance.export_tokens, reserved_input + 4096)
        result = export_metadata(product, adapter, adapter.config, [])
        self.assertEqual(result.specification.entrypoint, 'source_summary')
        call = adapter.calls[-1]
        self.assertEqual([{k: m[k] for k in ('role', 'content')} for m in call['request']['messages']],
                         reserved_messages)
        self.assertEqual(call['estimated_input_tokens'], reserved_input)
        text = call['request']['messages'][-1]['content']
        self.assertNotIn('FULL_DEVELOPMENT_EVIDENCE', text)
        sample = json.loads(text)['observed_execution']
        self.assertTrue(sample['result']['truncated'])
        self.assertNotIn('stdout', sample)
        self.assertLess(len(text), 20000)
        self.assertEqual(product.result, execution)

    def test_input_estimate_stops_oversized_first_request_before_provider_call(self):
        provider = RecordingModel(lambda *_: response('ok'))
        adapter = self.adapter(provider)
        with self.assertRaisesRegex(BudgetExceeded, 'estimated_input'):
            adapter.chat([{'role': 'user', 'content': 'x' * 200000}])
        self.assertEqual(provider.requests, [])

    def test_export_reserve_is_available_to_export_but_not_construction(self):
        provider = RecordingModel(lambda *_: response('ok'))
        adapter = self.adapter(provider, export_token_reserve=49000)
        adapter.allowance.exportable_tools = 1
        adapter.allowance.export_tokens = 49000
        with self.assertRaises(ExportBudgetReserved):
            adapter.chat(MESSAGES)
        self.assertEqual(provider.requests, [])
        self.assertEqual(adapter.scoped('export_metadata').chat(MESSAGES), 'ok')
        self.assertEqual(adapter.calls[-1]['export_reserved_tokens'], 0)
        self.assertGreater(adapter.calls[-1]['estimated_input_tokens'], 0)

    def test_no_final_products_means_no_export_reserve(self):
        provider = RecordingModel(lambda *_: response('ok'))
        adapter = self.adapter(provider, export_token_reserve=49000)
        adapter.allowance.track_pairs = True
        adapter.update_export_allowance([])
        self.assertEqual(adapter.chat(MESSAGES), 'ok')
        self.assertEqual(adapter.calls[-1]['export_reserved_tokens'], 0)
        self.assertGreater(adapter.calls[-1]['judgment_reserved_tokens'], 0)

    def test_recorded_remaining_72851_allows_current_judgment(self):
        for completed_products in (0, 1):
            provider = RecordingModel(lambda *_: response('ok'))
            adapter = self.adapter(provider)
            adapter.config = adapter.config.model_copy(update={'max_tokens': 300000, 'export_token_reserve': 50000})
            adapter.allowance.exportable_tools = completed_products
            adapter.allowance.export_tokens = 50000 if completed_products else 0
            adapter.record.add_usage(ResourceUsage(input_tokens=220297, output_tokens=6852, total_tokens=227149,
                model_calls=7, tool_calls=3, elapsed_seconds=300, usage_complete=True, usage_sources=['simulated']))
            judge = adapter.scoped('judgment')
            with self.subTest(completed_products=completed_products), patch.object(judge, 'estimate_input_tokens', return_value=41693):
                self.assertEqual(judge.chat(MESSAGES), 'ok')
            self.assertEqual(len(provider.requests), 1)
            self.assertEqual(judge.calls[-1]['export_reserved_tokens'], 0)

    def test_repair_is_not_started_if_request_fits_but_its_judgment_does_not(self):
        provider = RecordingModel(lambda *_: response('must not be called'))
        adapter = self.adapter(provider)
        adapter.allowance.track_pairs = True
        adapter.record.add_usage(ResourceUsage(input_tokens=40000, output_tokens=0, total_tokens=40000,
            model_calls=1, tool_calls=0, elapsed_seconds=1, usage_complete=True, usage_sources=['simulated']))
        repair = adapter.scoped('repair')
        with patch.object(repair, 'estimate_input_tokens', return_value=1000), self.assertRaisesRegex(BudgetExceeded, 'request and judgment'):
            repair.chat(MESSAGES)
        self.assertFalse(provider.requests)
        self.assertEqual(adapter.charged_tokens(), 40000)

    def test_generation_reserves_a_model_call_for_judgment(self):
        provider = RecordingModel(lambda *_: response('must not be called'))
        adapter = self.adapter(provider, max_model_calls=1)
        adapter.allowance.track_pairs = True
        with self.assertRaisesRegex(BudgetExceeded, 'model_calls_remaining=1'):
            adapter.chat(MESSAGES)
        self.assertFalse(provider.requests)

    def test_dynamic_export_allowance_tracks_only_selected_completed_products(self):
        from plugins.evolution.oscopilot.constructed import ConstructedTool
        product = ConstructedTool('tool', 'def tool(): return 1', 'tool()', 'A tool', '', None, {})
        adapter = self.adapter(RecordingModel(lambda *_: response('ok')), export_token_reserve=49000)
        with patch.object(adapter, 'estimate_input_tokens', return_value=1000):
            adapter.update_export_allowance([product])
            first = adapter.allowance.export_tokens
            adapter.update_export_allowance([product, product])
        self.assertEqual(first, 5096)
        self.assertEqual(adapter.allowance.export_tokens, first * 2)
        self.assertIs(adapter.scoped('repair').allowance, adapter.allowance)
        adapter.update_export_allowance([])
        self.assertEqual(adapter.allowance.export_tokens, 0)

    def test_actual_exhaustion_message_does_not_claim_timeout_reservations(self):
        provider = RecordingModel(lambda *_: response('ok'))
        adapter = self.adapter(provider)
        adapter.record.add_usage(ResourceUsage(input_tokens=50000, output_tokens=0, total_tokens=50000,
            model_calls=1, tool_calls=0, elapsed_seconds=1, usage_complete=True, usage_sources=['simulated']))
        with self.assertRaises(BudgetExceeded) as raised:
            adapter.chat(MESSAGES)
        self.assertIn('charged=50000', str(raised.exception))
        self.assertNotIn('timeout', str(raised.exception))
        self.assertEqual(provider.requests, [])

    def adapter(self, provider, *, deadline=None, **settings):
        record, calls = SessionRecord(), []
        config = OSCopilotConfig(max_tokens=50000, **settings)
        return ModelAdapter(provider, context(model_timeout_seconds=180, max_output_tokens=4096),
            config, record, calls, monotonic() + 1200 if deadline is None else deadline, purpose="construction")

    def test_timeout_recovers_and_later_stages_share_reserved_unknown_usage(self):
        def generate(query, ctx):
            if len(provider.requests) == 1:
                raise timeout(ctx)
            return response("generated code")
        provider = RecordingModel(generate)
        adapter = self.adapter(provider, model_timeout_seconds=180)
        with patch("plugins.evolution.oscopilot.model_adapter.sleep") as sleep:
            self.assertEqual(adapter.chat(MESSAGES), "generated code")
            self.assertEqual(adapter.scoped("judgment").chat(MESSAGES), "generated code")
        sleep.assert_called_once_with(2)
        self.assertEqual(len(provider.requests), 3)
        self.assertEqual(provider.requests[0][0], provider.requests[1][0])
        self.assertEqual(provider.requests[0][1].model_timeout_seconds, 180)
        self.assertNotEqual(provider.requests[0][1].operation_id, provider.requests[1][1].operation_id)
        self.assertEqual([item["attempt"] for item in adapter.calls], [1, 2, 1])
        self.assertEqual(adapter.calls[0]["logical_call_id"], adapter.calls[1]["logical_call_id"])
        usage = adapter.record.usage()
        self.assertEqual(usage.model_calls, 3)
        self.assertIsNone(usage.total_tokens)
        self.assertFalse(usage.usage_complete)
        self.assertEqual(len(usage.pending_reservations), 1)
        self.assertEqual(adapter.charged_tokens(), usage.pending_reservations[0].reserved_tokens + 10)
        self.assertEqual(adapter.calls[0]["reserved_tokens"], usage.pending_reservations[0].reserved_tokens)

    def test_retry_limit_is_three_physical_attempts_with_exponential_backoff(self):
        def generate(query, ctx):
            raise timeout(ctx)
        provider = RecordingModel(generate)
        adapter = self.adapter(provider)
        with patch("plugins.evolution.oscopilot.model_adapter.sleep") as sleep, self.assertRaises(OperationTimeout):
            adapter.chat(MESSAGES)
        self.assertEqual([item.args[0] for item in sleep.call_args_list], [2, 4])
        self.assertEqual(len(provider.requests), 3)
        self.assertEqual(adapter.record.usage().model_calls, 3)
        self.assertEqual(len(adapter.record.usage().pending_reservations), 3)

    def test_known_failed_usage_is_charged_once_and_can_recover(self):
        def generate(query, ctx):
            if len(provider.requests) == 1:
                raise timeout(ctx, known=True)
            return response("ok")
        provider = RecordingModel(generate)
        adapter = self.adapter(provider)
        with patch("plugins.evolution.oscopilot.model_adapter.sleep"):
            self.assertEqual(adapter.chat(MESSAGES), "ok")
        self.assertEqual(adapter.record.usage().total_tokens, 10)
        self.assertTrue(adapter.record.usage().usage_complete)
        self.assertEqual(adapter.record.usage().pending_reservations, ())

    def test_non_timeout_and_disabled_truncation_recovery_are_not_retried(self):
        for truncated in (False, True):
            def generate(query, ctx):
                if truncated:
                    return response("incomplete").replace(finish_reason="length")
                raise ExecutionError("Qwen request failed (HTTP 401)", stage=ctx.stage)
            provider = RecordingModel(generate)
            adapter = self.adapter(provider, max_output_expansions=0)
            with self.subTest(truncated=truncated), self.assertRaises((ExecutionError, ModelOutputTruncated)), \
                 patch("plugins.evolution.oscopilot.model_adapter.sleep") as sleep:
                adapter.chat(MESSAGES)
            self.assertEqual(len(provider.requests), 1)
            sleep.assert_not_called()

    def test_truncated_output_expands_and_resends_identical_full_input(self):
        provider = RecordingModel(lambda *_: response("cut off" if len(provider.requests) == 1 else "complete").replace(
            finish_reason="length" if len(provider.requests) == 1 else "stop"))
        adapter = self.adapter(provider, max_output_tokens=1024, max_expanded_output_tokens=4096)
        adapter = adapter.scoped("repair", evidence={"full_trace": "evidence" * 100})
        self.assertEqual(adapter.chat(MESSAGES), "complete")
        self.assertEqual([q.max_output_tokens for q, _ in provider.requests], [1024, 2048])
        self.assertEqual(provider.requests[0][0].messages, provider.requests[1][0].messages)
        self.assertEqual(adapter.calls[0]["error"]["code"], "model_output_truncated")
        self.assertEqual(adapter.calls[0]["next_output_tokens"], 2048)
        self.assertEqual(adapter.record.usage().total_tokens, 10)
        self.assertEqual(adapter.record.usage().model_calls, 2)

    def test_truncation_recovery_respects_expansion_outer_output_and_call_limits(self):
        cases = ({"max_output_expansions": 0}, {"max_expanded_output_tokens": 4096},
                 {"max_model_calls": 1}, {"max_output_expansions": 1})
        for settings in cases:
            provider = RecordingModel(lambda *_: response("cut off").replace(finish_reason="length"))
            adapter = self.adapter(provider, **settings)
            with self.subTest(settings=settings), self.assertRaises((ModelOutputTruncated, BudgetExceeded)):
                adapter.chat(MESSAGES)
            # Outer context caps at 4096, so no request with an unchanged output limit is retried.
            self.assertEqual(len(provider.requests), 1)

    def test_repeated_truncation_stops_after_the_expanded_response_and_accounts_both_calls(self):
        provider = RecordingModel(lambda *_: response("cut off").replace(finish_reason="length"))
        adapter = self.adapter(provider, max_output_tokens=1024)
        with self.assertRaises(ModelOutputTruncated):
            adapter.chat(MESSAGES)
        self.assertEqual([q.max_output_tokens for q, _ in provider.requests], [1024, 2048])
        self.assertEqual(adapter.record.usage().total_tokens, 10)

    def test_timeout_retry_and_output_expansion_have_separate_limits(self):
        def generate(query, ctx):
            if len(provider.requests) == 1:
                raise timeout(ctx)
            return response("cut off" if len(provider.requests) == 2 else "complete").replace(
                finish_reason="length" if len(provider.requests) == 2 else "stop")
        provider = RecordingModel(generate)
        adapter = self.adapter(provider, max_output_tokens=1024, max_model_retries=1)
        with patch("plugins.evolution.oscopilot.model_adapter.sleep"):
            self.assertEqual(adapter.chat(MESSAGES), "complete")
        self.assertEqual([q.max_output_tokens for q, _ in provider.requests], [1024, 1024, 2048])
        self.assertEqual(adapter.record.usage().model_calls, 3)
        self.assertFalse(adapter.record.usage().usage_complete)

    def test_retry_respects_call_and_reserved_token_limits(self):
        for setting, value in (("max_model_calls", 1), ("max_tokens", 5000)):
            def generate(query, ctx):
                raise timeout(ctx)
            provider = RecordingModel(generate)
            adapter = self.adapter(provider, max_model_calls=1) if setting == "max_model_calls" else self.adapter(provider)
            if setting == "max_tokens":
                adapter.config = adapter.config.model_copy(update={setting: value})
            with self.subTest(limit=setting), patch("plugins.evolution.oscopilot.model_adapter.sleep"), \
                 self.assertRaises(BudgetExceeded):
                adapter.chat(MESSAGES)
            self.assertEqual(len(provider.requests), 1)
            self.assertFalse(adapter.record.usage().usage_complete)

    def test_elapsed_budget_prevents_sleep_and_expired_budget_prevents_model_call(self):
        clock = [10.0]
        def generate(query, ctx):
            clock[0] = 13.0
            raise timeout(ctx)
        provider = RecordingModel(generate)
        adapter = self.adapter(provider, deadline=14.0)
        with patch("plugins.evolution.oscopilot.model_adapter.monotonic", side_effect=lambda: clock[0]), \
             patch("plugins.evolution.oscopilot.model_adapter.sleep") as sleep, self.assertRaises(BudgetExceeded):
            adapter.chat(MESSAGES)
        self.assertEqual(provider.requests[0][1].model_timeout_seconds, 4)
        sleep.assert_not_called()
        clock[0] = 15.0
        with patch("plugins.evolution.oscopilot.model_adapter.monotonic", side_effect=lambda: clock[0]), \
             self.assertRaises(BudgetExceeded):
            adapter.chat(MESSAGES)
        self.assertEqual(len(provider.requests), 1)

    def test_retries_can_be_disabled_and_outer_timeout_caps_inner_timeout(self):
        def generate(query, ctx):
            raise timeout(ctx)
        provider = RecordingModel(generate)
        adapter = self.adapter(provider, max_model_retries=0, model_timeout_seconds=240)
        with self.assertRaises(OperationTimeout):
            adapter.chat(MESSAGES)
        self.assertEqual(len(provider.requests), 1)
        self.assertEqual(provider.requests[0][1].model_timeout_seconds, 180)
