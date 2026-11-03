"""Responses transport metadata for the pinned native SkillClaw collector.

The upstream native Responses recorder omits tool calls/results and read_skills.
This adapter enriches that recorder using upstream protocol/metadata helpers.
It never imports AER task records or scores and does not change the workflow.
"""

import hashlib
import json
from pathlib import Path

from skillclaw import api_server as native
from skillclaw.protocols.openai_responses import normalize_content_to_text, to_openai_body


def output_texts(value, depth=0):
    """Unwrap JSON tool envelopes without interpreting code or escaped prose."""
    if depth > 8:
        return []
    if isinstance(value, dict):
        return [text for item in value.values() for text in output_texts(item, depth + 1)]
    if isinstance(value, list):
        return [text for item in value for text in output_texts(item, depth + 1)]
    if not isinstance(value, str):
        return []
    result = [value.replace("\r\n", "\n")]
    # Code-mode outputs can prefix JSON text with execution timing metadata.
    for line in value.splitlines():
        if line.lstrip().startswith(("{", "[")):
            try:
                decoded = json.loads(line)
            except ValueError:
                continue
            result.extend(output_texts(decoded, depth + 1))
    return result


class ResponsesCollector(native.SkillClawAPIServer):
    def _record_responses_turn(self, session_id, request_body, response_payload, **kwargs):
        if not session_id:
            return
        # Convert only recording metadata, never the request sent to the task model.
        inputs = request_body.get("input", [])
        if isinstance(inputs, list):
            inputs = [{**item, "type": "function_call_output",
                       "output": normalize_content_to_text(item.get("output", ""))}
                      if isinstance(item, dict) and item.get("type") in {
                          "function_call_output", "custom_tool_call_output"} else item for item in inputs]
        messages = to_openai_body({"input": inputs}, self._served_model)["messages"]
        turns = self._session_turns.get(session_id, [])
        # A request can contain full history. Attach each observation to the
        # original call by ID, including parallel calls and incremental inputs.
        for turn in turns:
            calls = turn.get("tool_calls", [])
            ids = {call["id"] for call in calls}
            results = [message for message in messages if message.get("role") == "tool"
                       and message.get("tool_call_id") in ids]
            if results:
                chat = [{"role": "assistant", "content": "", "tool_calls": calls}, *results]
                native._merge_tool_error_info(turn, native._extract_recent_tool_results(chat), results)
                self._record_verified_reads(turn, results)
        super()._record_responses_turn(session_id, request_body, response_payload, **kwargs)
        turn = self._session_turns[session_id][-1]
        calls = []
        for item in response_payload.get("output", []):
            if item.get("type") not in {"function_call", "custom_tool_call"}:
                continue
            arguments = item.get("arguments", item.get("input", ""))
            calls.append({"id": item.get("call_id") or item.get("id"), "type": "function",
                "function": {"name": item.get("name", ""), "arguments": arguments
                    if isinstance(arguments, str) else json.dumps(arguments)}})
        content = "\n".join(normalize_content_to_text(item.get("content", []))
                            for item in response_payload.get("output", []) if item.get("type") == "message")
        turn.update(tool_calls=calls, tool_results=native._build_tool_summaries(calls),
            tool_results_raw=[], tool_observations=[], tool_errors=[], read_skills=[], modified_skills=[],
            raw_turn_kind=native._classify_raw_turn_kind("responses_compat", content, calls),
            prompt_text=native._extract_last_user_instruction(messages),
            response_text=content or json.dumps(calls, ensure_ascii=False),
            aer_read_receipts=[], aer_read_evidence="full_content_in_tool_output")

    def _record_verified_reads(self, turn, results):
        """Conservative evidence: complete current file text appears in tool output.

        Listing a path or proposing a read is insufficient. Partial/truncated
        reads remain unverified; absence of receipts does not prove no read.
        """
        if not self.skill_manager:
            return
        paths = self.skill_manager.get_skill_path_map()
        receipts = turn.setdefault("aer_read_receipts", [])
        seen = {(receipt["path"], receipt["call_id"]) for receipt in receipts}
        for result in results:
            outputs = output_texts(result.get("content", ""))
            for filename in paths:
                path = Path(filename)
                # Short ancillary files (e.g. a config containing "1") can
                # coincidentally occur in unrelated output. Only the complete
                # skill entrypoint supports body-exposure attribution here.
                if path.name != "SKILL.md" or not path.is_file():
                    continue
                data = path.read_bytes()
                try:
                    body = data.decode("utf-8").replace("\r\n", "\n").strip()
                except UnicodeDecodeError:
                    continue
                identity = (str(path), result.get("tool_call_id"))
                if not body or not any(body in output for output in outputs) or identity in seen:
                    continue
                # Reuse native path-to-skill mapping, preserving its schema.
                refs = native._extract_read_skills_from_tool_calls([{"function": {
                    "name": "read_file", "arguments": json.dumps({"path": str(path)})}}], paths)
                for ref in refs:
                    if ref not in turn["read_skills"]:
                        turn["read_skills"].append(ref)
                receipts.append({"path": str(path), "call_id": result.get("tool_call_id"),
                    "sha256": hashlib.sha256(data).hexdigest(), "skills": refs})
                seen.add(identity)
