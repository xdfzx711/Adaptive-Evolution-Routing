"""Wire-level JSON mode is confined to OS-Copilot metadata requests."""

import json
import unittest
import httpx

from tests.execution import test_qwen
from tests.evolution import test_oscopilot_model


class MetadataTransportTests(unittest.TestCase):
    def test_only_metadata_scopes_enable_json_mode_and_keep_usage(self):
        sent = []

        def handler(request):
            sent.append(json.loads(request.content))
            return httpx.Response(200, json=test_qwen.completion('{"ok": true}'))

        provider = test_qwen.QwenTests().provider(handler)
        adapter = test_oscopilot_model.OSCopilotModelTests().adapter(provider)
        messages = [{"role": "user", "content": "Return JSON metadata."}]
        for purpose in ("construction", "export_metadata", "export_metadata_format", "repair", "judgment"):
            adapter.scoped(purpose, include_evidence=False).chat(messages)
        self.assertEqual([p.get("response_format") for p in sent],
                         [None, {"type": "json_object"}, {"type": "json_object"}, None, None])
        self.assertIsNone(provider.config.response_format)
        self.assertEqual(adapter.record.usage().model_calls, 5)
        self.assertEqual(adapter.charged_tokens(), 625)
        self.assertEqual(adapter.calls[2]["purpose"], "export_metadata_format")

    def test_text_fallback_keeps_shared_provider_setting_unchanged(self):
        sent = []

        def handler(request):
            sent.append(json.loads(request.content))
            return httpx.Response(200, json=test_qwen.completion())

        provider = test_qwen.QwenTests().provider(handler, response_format="json_object")
        adapter = test_oscopilot_model.OSCopilotModelTests().adapter(provider, export_response_format="text")
        adapter.scoped("export_metadata").chat([{"role": "user", "content": "Return JSON."}])
        self.assertNotIn("response_format", sent[0])
        self.assertEqual(provider.config.response_format, "json_object")
