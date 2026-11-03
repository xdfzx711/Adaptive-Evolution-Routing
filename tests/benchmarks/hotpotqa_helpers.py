import json
from pathlib import Path

import yaml

from experiments.prepare_hotpotqa import prepare_manifest


ROOT = Path(__file__).resolve().parents[2]


def fixture(root, *, tasks=2):
    root = Path(root)
    rows = []
    for i in range(20):
        rows.append({"_id": f"question-{i:02d}", "question": f"Question {i}?",
            "answer": f"PRIVATE_ANSWER_{i}", "supporting_facts": [[f"Doc {i}", 0]],
            "context": [[f"Doc {i}", [f"Public evidence {i}.", "A distractor sentence."]]],
            "type": "bridge" if i % 2 else "comparison", "level": "hard"})
    train, dev = root / "train.json", root / "dev.json"
    train.write_text(json.dumps(rows[:16]), encoding="utf-8")
    dev.write_text(json.dumps(rows[16:]), encoding="utf-8")
    manifest_path = root / "manifest.json"
    manifest = prepare_manifest(train, dev, manifest_path, task_count=tasks, test_count=2)
    return rows, manifest_path, manifest


def config_file(root, *, tasks=2):
    rows, manifest_path, manifest = fixture(root, tasks=tasks)
    data = yaml.safe_load((ROOT / "experiments/configs/v0_1_fixed_prompt.yaml").read_text(encoding="utf-8"))
    data["benchmark"] = {"plugin": "hotpotqa_adapter", "execution_split": "dev", "task_ids": None,
                         "config": {"manifest_path": str(manifest_path)}}
    data["agent"] = {"plugin": "single_call_agent", "max_steps": 1, "initial_prompt": "BAD initial prompt",
                     "initial_snapshot_ref": None, "config": {"skills": None, "memory": None}}
    data["assessment"] = {"evaluator": "hotpotqa_evaluator", "evaluator_config": {"primary_metric": "joint_f1"},
                          "diagnoser": "lightweight_diagnoser"}
    data["evolution"]["stop_on_success"] = False
    data["targets"].update(default_operators={"prompt": "gepa_prompt_evolver"}, operator_configs={"prompt": {
        "max_metric_calls": 6, "max_iterations": 1, "samples_per_evaluation": 3,
        "reflection_model": data["providers"]["model"]}})
    data["validation"].update(acceptance_policy="weighted_improvement",
        weights={"current_task": 0.5, "similar": 0.25, "regression": 0.25})
    data["limits"]["max_evolution_rounds"] = 1
    data["storage"]["root"] = str(root / "runs")
    path = root / "config.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return path, rows, manifest
