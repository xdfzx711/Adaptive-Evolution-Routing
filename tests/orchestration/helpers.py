import json
from pathlib import Path

import yaml

from experiments.orchestration import prepare_orchestrator
from plugins.providers.scripted import ScriptedModel
from tests.execution.helpers import MODEL, RecordingModel, response


ROOT = Path(__file__).resolve().parents[2]


def configured(root, *, mode="fixed", rounds=2, task_mode="independent", single=False, candidates=1):
    filename = "v0_1_no_evolution.yaml" if mode == "disabled" else "v0_1_manual_prompt.yaml" if mode == "manual" else "v0_1_fixed_prompt.yaml"
    config = yaml.safe_load((ROOT / "experiments/configs" / filename).read_text(encoding="utf-8"))
    config["storage"]["root"] = str(root / "runs")
    config["limits"]["max_evolution_rounds"] = 0 if mode == "disabled" else rounds
    config["agent"]["max_steps"] = 1
    config["state"]["task_mode"] = task_mode
    config["evolution"]["max_candidates_per_round"] = candidates
    if single:
        config["agent"]["plugin"] = "single_call_agent"
    if mode == "manual":
        config["routing"]["manual_decisions"] = config["routing"]["manual_decisions"][:min(1, rounds)]
    path = root / "config.yaml"
    path.write_text(yaml.safe_dump(config), encoding="utf-8")
    return path, prepare_orchestrator(path, run_id="controller")


def attach_model(task, *, initial_success=False, empty=False, always_wrong=False, local_failure=False,
                 post_commit_error=False, unknown_validation=False, generation_error=False, candidates=1, mixed=False):
    scripted = ScriptedModel(MODEL)
    actual_usage = []
    def answer(req, ctx):
        user = json.loads(req.messages[-1].content)
        if user.get("operation") == "prompt_evolution":
            if generation_error:
                raise RuntimeError("fixture provider failure")
            proposals = [] if empty else [{"prompt": f"IMPROVED reusable instructions round {user['context']['round_id']} candidate {index}"}
                                           for index in range(candidates)]
            result = response(json.dumps({"candidates": proposals}))
        else:
            improved = req.messages[0].content.startswith("IMPROVED")
            if post_commit_error and ctx.stage == "post_commit_execution":
                raise RuntimeError("fixture retest provider failure")
            wrong = always_wrong or not improved and not initial_success or local_failure and user["public_metadata"]["split"] == "dev"
            result = response('{"answer":-999}') if wrong else scripted.generate(req.replace(tools=[]), ctx)
            if ctx.stage.endswith("_validation") and improved and (unknown_validation or mixed and "candidate 1" in req.messages[0].content):
                result = result.replace(usage=response(unknown=True).usage)
        actual_usage.append(result.usage)
        return result
    model = RecordingModel(answer)
    task.execution.agent.model = model
    if task.evolution:
        task.evolution.resolver.resolve("prompt").operator.model = model
    return model, actual_usage
