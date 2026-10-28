from datetime import datetime, timezone
from time import monotonic

from aer_core.schemas.optimization import OptimizationCandidate
from .archive import prompt_key


def now():
    return datetime.now(timezone.utc)


class SearchLimit(Exception):
    pass


class SearchRecord:
    """GEPA callbacks retain decisions without GEPA's separate log files."""

    def __init__(self, prompt, config, *, seen_prompts=None, round_id=1):
        self.started_at, self.start = now(), monotonic()
        self.config = config
        self.candidates = [OptimizationCandidate(candidate=1, prompt=prompt)]
        self.trials, self.reflections, self.decisions = [], [], []
        self.iterations = 0
        self.stop_reason = "completed"
        self.failure = None
        self.round_id = round_id
        self.seen_prompts = seen_prompts if seen_prompts is not None else {}
        self.seen_prompts.setdefault(prompt_key(prompt), round_id)
        self.duplicate_exhausted = False

    def duplicate_round(self, prompt):
        return self.seen_prompts.get(prompt_key(prompt)) if self.config.deduplicate_prompts else None

    def reserve_proposal(self, prompt):
        self.seen_prompts[prompt_key(prompt)] = self.round_id

    def number(self, prompt):
        for item in self.candidates:
            if item.prompt == prompt:
                return item.candidate
        number = len(self.candidates) + 1
        self.candidates.append(OptimizationCandidate(candidate=number, prompt=prompt))
        return number

    def remaining_seconds(self):
        return max(0.0, self.config.timeout_seconds - (monotonic() - self.start))

    def check_limit(self, required_calls=1):
        if self.remaining_seconds() <= 0:
            self.stop_reason = "timeout"
            raise SearchLimit("timeout")
        if len(self.trials) + required_calls > self.config.max_metric_calls:
            self.stop_reason = "metric_limit"
            raise SearchLimit("metric_limit")

    def stop(self, state):
        if self.duplicate_exhausted:
            self.stop_reason = "duplicate_prompt"
            return True
        if self.failure is not None:
            self.stop_reason = "error"
            return True
        if self.remaining_seconds() <= 0:
            self.stop_reason = "timeout"
            return True
        if self.iterations >= self.config.max_iterations:
            self.stop_reason = "iteration_limit"
            return True
        if len(self.trials) + self.config.samples_per_evaluation > self.config.max_metric_calls:
            self.stop_reason = "metric_limit"
            return True
        return False

    def on_iteration_start(self, event):
        self.iterations += 1

    def on_candidate_selected(self, event):
        self.parent = self.number(event["candidate"]["prompt"])
        self.decisions.append({"event": "selected_parent", "iteration": self.iterations,
                               "candidate": self.parent, "score": event["score"]})

    def on_proposal_end(self, event):
        number = self.number(event["new_instructions"]["prompt"])
        self.child = number
        item = self.candidates[number - 1]
        if number != getattr(self, "parent", 1):
            self.candidates[number - 1] = item.replace(parents=[getattr(self, "parent", 1)])
        self.decisions.append({"event": "generated", "iteration": self.iterations, "candidate": number})

    def on_valset_evaluated(self, event):
        number = self.number(event["candidate"]["prompt"])
        self.candidates[number - 1] = self.candidates[number - 1].replace(score=event["average_score"])

    def on_candidate_accepted(self, event):
        self.decisions.append({"event": "accepted_in_search", "iteration": self.iterations,
            "candidate": getattr(self, "child", None), "score": event["new_score"]})

    def on_candidate_rejected(self, event):
        self.decisions.append({"event": "rejected_in_search", "iteration": self.iterations,
            "candidate": getattr(self, "child", None),
            "baseline_score": event["old_score"], "candidate_score": event["new_score"], "reason": event["reason"]})

    def apply_result(self, result):
        for index, candidate in enumerate(result.candidates):
            number = self.number(candidate["prompt"])
            parents = [self.number(result.candidates[parent]["prompt"]) for parent in result.parents[index]
                       if parent is not None and result.candidates[parent]["prompt"] != candidate["prompt"]]
            self.candidates[number - 1] = self.candidates[number - 1].replace(
                score=result.val_aggregate_scores[index], parents=parents)


class QuietLogger:
    def __init__(self, record):
        self.record = record

    def log(self, message):
        if "plugins.evolution.gepa.recording.SearchLimit:" in message:
            return  # Normal stop reason is already recorded without a stack trace.
        # Keep diagnostic library messages in the existing record, without
        # duplicating normal progress or printing a second console format.
        if any(word in message.lower() for word in ("failed", "error", "traceback", "did not propose")):
            self.record.decisions.append({"event": "optimizer_message", "iteration": self.record.iterations,
                                          "message": message})
