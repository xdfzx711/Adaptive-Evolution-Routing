"""Offline CLI fixture that makes real HTTP requests through the native proxy."""

import json
import os
from pathlib import Path
import sys
import tomllib
from urllib.request import Request, urlopen

if "--version" in sys.argv:
    print("codex-cli 0.162.1")
    raise SystemExit

config = {}
for index, value in enumerate(sys.argv):
    if value == "--config":
        entry = tomllib.loads(sys.argv[index + 1])
        if "model_providers" in entry:
            config.update(entry["model_providers"]["aer_skillclaw"])
task = json.loads(sys.stdin.read())
model = sys.argv[sys.argv.index("--model") + 1]
headers = {"Content-Type": "application/json", "Authorization": "Bearer " + os.environ[config["env_key"]],
           **config["http_headers"]}

def send(inputs):
    body = json.dumps({"model": model, "input": inputs, "stream": False}).encode()
    with urlopen(Request(config["base_url"] + "/responses", body, headers), timeout=20) as response:
        return json.load(response)

inputs = [{"role": "user", "content": json.dumps(task)}]
first = send(inputs)["output"][0]
arguments = json.loads(first["arguments"])
skill = Path(arguments["path"])
content = skill.read_text(encoding="utf-8")
if "--no-read" in sys.argv:
    content = "File read failed"
if "--modify-skill" in sys.argv:
    skill.write_text(content + "tampered", encoding="utf-8")
inputs += [first, {"type": "function_call_output", "call_id": first["call_id"], "output": content}]
final = send(inputs)["output"][0]["content"][0]["text"]
for event in ({"type": "thread.started", "thread_id": "fixture"}, {"type": "turn.started"},
              {"type": "item.completed", "item": {"id": "final", "type": "agent_message", "text": final}},
              {"type": "turn.completed", "usage": {"input_tokens": 20, "output_tokens": 10}}):
    print(json.dumps(event), flush=True)
