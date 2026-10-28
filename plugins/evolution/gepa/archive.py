"""Plugin-local candidate memory; never shared across tasks or execution setups."""

import json


def prompt_key(prompt):
    return "\n".join(line.rstrip() for line in prompt.replace("\r\n", "\n").replace("\r", "\n").split("\n")).strip()


class PromptArchive:
    def __init__(self):
        self.scopes = {}

    def for_request(self, request):
        snapshot = request.context.stable_snapshot
        key = (request.run_context.run_id, request.run_context.episode_id,
               request.context.task.benchmark_id, request.context.task.task_id,
               snapshot.agent_plugin_id,
               json.dumps(snapshot.model_dump(mode="json")["config"], sort_keys=True),
               tuple(sorted((kind, ref.digest) for kind, ref in snapshot.artifacts.items() if kind != "prompt")))
        return self.scopes.setdefault(key, {})
