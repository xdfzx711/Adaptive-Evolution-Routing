"""Controlled model responses for the real HotpotQA/tool construction/MCP pipeline."""

from contextlib import contextmanager
import json
from pathlib import Path
import sys
from unittest.mock import patch

import yaml

from experiments.configuration import load_experiment_config
from experiments.prepare_hotpotqa import prepare_manifest
from plugins.evolution.oscopilot.evolver import OSCopilotToolEvolver
from tests.evolution.test_oscopilot import native_plan, judgment
from tests.execution.helpers import MODEL, RecordingModel, response


ROOT = Path(__file__).resolve().parents[2]
BASE = ROOT / "experiments/configs/v0_1_codex_hotpotqa_gepa_300_r3.yaml"
SOURCE = '''def locate_evidence(context):
    for paragraph in context:
        for sentence in paragraph['sentences']:
            if sentence['text'].startswith('Answer: '):
                return {'answer': sentence['text'][8:],
                        'supporting_facts': [[paragraph['title'], sentence['sentence_id']]]}
    return {'answer': 'unknown', 'supporting_facts': []}
'''
INPUT = {"type": "object", "properties": {"context": {"type": "array", "items": {"type": "object"}}},
         "required": ["context"], "additionalProperties": False}
OUTPUT = {"type": "object", "properties": {"answer": {"type": "string"}, "supporting_facts": {"type": "array"}},
          "required": ["answer", "supporting_facts"]}
EXAMPLE = {"context": [{"title": "Example", "sentences": [{"sentence_id": 3, "text": "Answer: example"}]}]}


def overrides():
    # Replace maps rather than retain incompatible GEPA prompt entries.
    return ["routing.fixed_target=tool", "targets.allowed=[tool]",
            "targets.default_operators={tool: oscopilot_tool_evolver}",
            "targets.operator_configs={tool: {max_generated_tools: 2, max_tokens: 200000, "
            "export_token_reserve: 20000, timeout_seconds: 900, model_timeout_seconds: 90, "
            "max_output_tokens: 4096, max_expanded_output_tokens: 16384}}",
            "agent.config.tooling={catalog: {}, generated_tools: {enabled: true, runtime: python_jupyter}}",
            "agent.config.initial_tools=[]", "limits.max_output_tokens_per_call=16384"]


def configured(root, *, tasks=1, mode="learn"):
    rows = [{"_id": f"question-{i:02d}", "question": f"What is the value of item {i}?",
             "answer": f"value-{i}", "supporting_facts": [[f"Document {i}", 1]],
             "context": [[f"Document {i}", ["An unrelated sentence. " * 120, f"Answer: value-{i}"]]],
             "type": "bridge" if i % 2 else "comparison", "level": "hard"} for i in range(24)]
    train, dev = root / "train.json", root / "dev.json"
    train.write_text(json.dumps(rows[:20]), encoding="utf-8")
    dev.write_text(json.dumps(rows[20:]), encoding="utf-8")
    manifest_path = root / "manifest.json"
    manifest = prepare_manifest(train, dev, manifest_path, task_count=tasks, similar_count=1, regression_count=1, test_count=1)
    data = yaml.safe_load(BASE.read_text(encoding="utf-8"))
    data["benchmark"]["config"]["manifest_path"] = str(manifest_path)
    data["providers"]["model"] = MODEL.model_dump(mode="json")
    data["assessment"]["diagnoser"] = "lightweight_diagnoser"
    data["agent"]["config"].update(command=[sys.executable, str(ROOT / "tests/execution/fake_codex_hotpotqa.py"),
        "--fixture-mode", mode], workspace_root=str(root / "workspaces"))
    data["limits"]["max_evolution_rounds"] = 1
    data["storage"]["root"] = str(root / "runs")
    # Fixture inputs are temporary; the shipped YAML is never edited.
    path = root / "config.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return load_experiment_config(path, overrides=overrides()), rows, manifest


def replies():
    interface = {"input_schema": INPUT, "output_schema": OUTPUT,
        "usage": {"capability_gap": "Locate explicitly marked evidence in supplied sentences.",
                  "use_when": "Pass the current task's context array.", "added_value": "Returns the original evidence reference.",
                  "result_usage": "Use answer and supporting_facts in the final JSON.",
                  "preconditions": "Synthetic marked sentences only; no file or network access.", "cost": "Local linear scan."},
        "runtime_outcome": {"success_schema": OUTPUT, "failure_schema": {"type": "object", "required": ["error"]}},
        "acceptance": {"kind": "json", "success_schema": {"type": "object", "properties": {
            "answer": {"const": "example"}, "supporting_facts": {"const": [["Example", 3]]}},
            "required": ["answer", "supporting_facts"]}}}
    spec = {"description": "Locate marked evidence in caller-supplied sentences.", "entrypoint": "locate_evidence",
            "input_schema": INPUT, "example_arguments": EXAMPLE}
    return [native_plan({"locate_evidence": {"description": "Locate evidence in supplied JSON paragraphs, preserving references.",
                 "dependencies": [], "type": "Python", "deliverable": True}}),
            "```python\n" + SOURCE + "```\n<invoke>locate_evidence(" + repr(EXAMPLE["context"]) + ")</invoke>",
            judgment(), json.dumps({"specification": spec, "interface": interface})]


@contextmanager
def offline_construction(responses_factory=replies):
    original = OSCopilotToolEvolver.evolve
    models = []

    def evolve(operator, request):
        responses = iter(responses_factory())
        model = RecordingModel(lambda *_: response(next(responses)))
        models.append(model)
        operator.generator = operator.judge = model
        return original(operator, request)

    with patch.object(OSCopilotToolEvolver, "evolve", evolve):
        yield models


def offline_child(loaded, task_id, run_id, console, report_holdouts):
    from experiments.parallel_batch import _run_child
    with offline_construction():
        return _run_child(loaded, task_id, run_id, console, report_holdouts)
