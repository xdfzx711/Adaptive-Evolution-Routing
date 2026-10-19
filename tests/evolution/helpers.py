from aer_core.context import ContextBuilder, ContextPolicy
from aer_core.schemas.assessment import TaskExecutionRecord
from aer_core.schemas.execution import Diagnosis, ExecutionResult, TraceEvent
from aer_core.schemas.states import RunStage
from aer_core.schemas.usage import ResourceUsage
from plugins.benchmarks.toy_tasks import ToyBenchmark
from plugins.evaluators.deterministic import DeterministicEvaluator
from tests.execution.helpers import agent_fixture, context


class MemoryExperience:
    def __init__(self, events=()):
        self.items = list(events)

    def query(self, criteria):
        return tuple(self.items)


def fixture(single=False):
    benchmark = ToyBenchmark()
    task = benchmark.tasks("dev")[0]
    public = benchmark.task_view(task)
    agent, stable, artifacts = agent_fixture(single=single)
    result = ExecutionResult(task_id=task.task_id, agent_version_id=stable.version_id, operation_id="exec-test",
        output={"answer": 74}, trajectory=[TraceEvent(seq=0, kind="model_response", payload={"content": '{"answer":74}'}),
                                          TraceEvent(seq=1, kind="finish", payload={"output": {"answer": 74}})],
        state_summary={"model_steps": 1}, usage=ResourceUsage.zero(), status="completed")
    evaluator = DeterministicEvaluator()
    score = evaluator.evaluate_record(task, result, benchmark.evaluation_spec(task), operation_id="score-test")
    record = TaskExecutionRecord(run_id="run-1", episode_id="episode-1", task=task.identity, execution=result,
        evaluation=score, feedback=evaluator.feedback_view(score),
        diagnosis=Diagnosis(reason_code="incorrect_answer", explanation="public diagnosis", evidence_step_ids=[0]))
    live = context(stage=RunStage.EVOLUTION, max_output_tokens=2048)
    policy = ContextPolicy(allow_evolution=True, output_fields=["answer"], tool_arguments={"calculator": ["expression"]}, tool_outputs={"calculator": ["value"]})
    return agent, stable, artifacts, public, record, live, policy


def evolution_context(single=False, round_id=1):
    agent, stable, artifacts, public, record, live, policy = fixture(single)
    state = ContextBuilder(MemoryExperience(), policy).build(public, record, stable, live,
        round_id=round_id, feasible_targets=["prompt"])
    return state, artifacts, live
