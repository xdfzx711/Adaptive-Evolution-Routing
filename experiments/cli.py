"""Synchronous configuration, execution, evolution and task-controller CLI."""

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from aer_core import __version__
from aer_core.errors import ConfigError, PluginError

from .configuration import load_experiment_config
from .bootstrap import assemble_experiment
from .storage import prepare_storage
from .benchmark import load_execution, prepare_benchmark
from .execution import prepare_execution
from .environment import env_file_path
from .evolution import load_task_record, prepare_evolution
from .validation import load_proposal, prepare_validation
from .orchestration import prepare_orchestrator
from .reporting import summarize_saved_run
from .reorganize import reorganize_run
from plugins.storage._files import FileRoot


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="aer", description="AER experiment configuration tools")
    parser.add_argument("--version", action="version", version=f"AER {__version__}")
    subparsers = parser.add_subparsers(dest="command", required=True)
    validate = subparsers.add_parser("validate-config", help="validate YAML and print the resolved configuration")
    validate.add_argument("config", type=Path, help="path to an experiment YAML file")
    validate.add_argument("--benchmark-index", type=Path, help="optional public task index JSON; relative to the current directory")
    validate.add_argument("--output", type=Path, help="write resolved JSON to this file instead of stdout")
    assemble = subparsers.add_parser("assemble-config", help="construct configured Agent/Router/Evolver plugins and print their manifest")
    assemble.add_argument("config", type=Path, help="path to an experiment YAML file")
    assemble.add_argument("--benchmark-index", type=Path, help="optional public task index JSON")
    assemble.add_argument("--output", type=Path, help="write assembly manifest JSON to this file instead of stdout")
    storage = subparsers.add_parser("init-storage", help="create/reopen a persistent run directory and initialize its Snapshot")
    storage.add_argument("config", type=Path, help="path to an experiment YAML file")
    storage.add_argument("--run-id", help="optional run directory name; default uses local time")
    storage.add_argument("--benchmark-index", type=Path, help="optional public task index JSON")
    storage.add_argument("--output", type=Path, help="write storage initialization JSON to this file instead of stdout")
    benchmark = subparsers.add_parser("inspect-benchmark", help="load actual task partitions and print public tasks")
    benchmark.add_argument("config", type=Path)
    benchmark.add_argument("--output", type=Path)
    score = subparsers.add_parser("score-execution", help="score a saved ExecutionResult without executing an Agent")
    score.add_argument("config", type=Path)
    score.add_argument("execution", type=Path, help="serialized ExecutionResult JSON")
    score.add_argument("--operation-id", default="standalone-evaluation", help="evaluation operation identity")
    score.add_argument("--output", type=Path)
    execute = subparsers.add_parser("execute-task", help="execute and assess one task without evolution")
    execute.add_argument("config", type=Path)
    execute.add_argument("--task-id", required=True)
    execute.add_argument("--run-id")
    execute.add_argument("--output", type=Path)
    propose = subparsers.add_parser("propose-prompt", help="route one failed execution and generate isolated candidates")
    propose.add_argument("config", type=Path)
    propose.add_argument("record", type=Path, help="TaskExecutionRecord or execute-task JSON report")
    propose.add_argument("--run-id", required=True, help="existing execution run directory name")
    propose.add_argument("--round-id", type=int, required=True, help="explicit next round, starting at 1")
    propose.add_argument("--output", type=Path)
    validation = subparsers.add_parser("validate-proposal", help="validate, accept and select candidates without committing")
    validation.add_argument("config", type=Path)
    validation.add_argument("proposal", type=Path, help="EvolutionProposalRecord or propose-prompt JSON report")
    validation.add_argument("--run-id", required=True)
    validation.add_argument("--output", type=Path)
    run = subparsers.add_parser("run-task", help="run one task evolution episode, including validation and commit")
    run.add_argument("config", type=Path)
    run.add_argument("--task-id", required=True)
    run.add_argument("--run-id", help="optional run directory name; default uses local time")
    run.add_argument("--output", type=Path)
    run.add_argument("--quiet", action="store_true", help="suppress console progress; evolution.txt is still saved")
    run.add_argument("--json", action="store_true", help="print the full result JSON when --output is omitted")
    batch = subparsers.add_parser("run-benchmark", help="run selected benchmark tasks and save an incremental baseline summary")
    batch.add_argument("config", type=Path)
    batch.add_argument("--run-id")
    batch.add_argument("--limit", type=int)
    batch.add_argument("--output", type=Path)
    batch.add_argument("--quiet", action="store_true")
    batch.add_argument("--report-holdouts", action="store_true",
                       help="record initial/final holdout performance for every episode, including unchanged Prompts")
    batch.add_argument("--set", dest="overrides", action="append", default=[], metavar="PATH=VALUE")
    summary = subparsers.add_parser("summarize-run", help="create one structured evolution.txt from a saved run-task result without executing models")
    summary.add_argument("result", type=Path)
    reorganize = subparsers.add_parser("reorganize-run", help="move a completed legacy run into round directories without model calls")
    reorganize.add_argument("run_root", type=Path)
    for command in (validate, assemble, storage, benchmark, score, execute, propose, validation, run):
        command.add_argument("--set", dest="overrides", action="append", default=[], metavar="PATH=VALUE",
            help="override a YAML field before validation; repeat for multiple plugin/runtime settings")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(encoding="utf-8")
    args = build_parser().parse_args(argv)
    try:
        if args.command == "run-benchmark":
            from .batch import cli_run
            return cli_run(args)
        if args.command == "reorganize-run":
            print(json.dumps(reorganize_run(args.run_root), ensure_ascii=False, indent=2))
            return 0
        if args.command == "summarize-run":
            log_path = summarize_saved_run(args.result)
            print(log_path)
            return 0
        if args.overrides:
            args.config = load_experiment_config(args.config, benchmark_index=getattr(args, "benchmark_index", None),
                overrides=args.overrides)
        if args.command == "run-task":
            if not args.quiet:
                print("[准备] 正在加载配置、任务和插件…", file=sys.stderr, flush=True)
            orchestration_setup = prepare_orchestrator(args.config, run_id=args.run_id, console=not args.quiet)
            if not args.quiet:
                print(f"[输出位置] {orchestration_setup.execution.storage.run_root / 'evolution.txt'}", file=sys.stderr, flush=True)
            prepared_benchmark = orchestration_setup.execution.benchmark
            loaded = prepared_benchmark.loaded_config
            run_result = orchestration_setup.run_task(args.task_id)
            payload = orchestration_setup.save_record(run_result)
        elif args.command == "validate-proposal":
            validation_setup = prepare_validation(args.config, run_id=args.run_id)
            prepared_benchmark = validation_setup.execution.benchmark
            loaded = prepared_benchmark.loaded_config
            validation_result = validation_setup.validate(load_proposal(args.proposal))
            payload = validation_setup.save_record(validation_result)
        elif args.command == "propose-prompt":
            generation = prepare_evolution(args.config, run_id=args.run_id)
            prepared_benchmark = generation.execution.benchmark
            loaded = prepared_benchmark.loaded_config
            proposal = generation.propose(load_task_record(args.record), round_id=args.round_id)
            payload = generation.save_record(proposal)
        elif args.command == "execute-task":
            execution_setup = prepare_execution(args.config, run_id=args.run_id)
            prepared_benchmark = execution_setup.benchmark
            loaded = prepared_benchmark.loaded_config
            task_record = execution_setup.run_task(args.task_id)
            payload = execution_setup.save_record(task_record)
        elif args.command in {"inspect-benchmark", "score-execution"}:
            prepared_benchmark = prepare_benchmark(args.config)
            loaded = prepared_benchmark.loaded_config
            if args.command == "score-execution":
                execution = load_execution(args.execution)
                evaluation = prepared_benchmark.score(execution, operation_id=args.operation_id)
                payload = {"status": "scored", "dataset_version": prepared_benchmark.benchmark.dataset_version,
                           "scorer_version": prepared_benchmark.evaluator.scorer_version,
                           "evaluation": evaluation.model_dump(mode="json"),
                           "feedback": prepared_benchmark.public_feedback(evaluation).model_dump(mode="json")}
            else:
                payload = prepared_benchmark.as_dict()
        elif args.command == "init-storage":
            prepared = prepare_storage(args.config, run_id=args.run_id, benchmark_index=args.benchmark_index)
            loaded = prepared.loaded_config
            payload = prepared.as_dict()
        elif args.command == "assemble-config":
            assembled = assemble_experiment(args.config, benchmark_index=args.benchmark_index)
            loaded = assembled.loaded_config
            payload = assembled.as_dict()
        else:
            loaded = load_experiment_config(args.config, benchmark_index=args.benchmark_index)
            payload = loaded.as_dict()
    except ConfigError as error:
        print(error, file=sys.stderr)
        return 2
    except PluginError as error:
        label = ("Task controller error" if args.command == "run-task"
                 else "Validation setup error" if args.command == "validate-proposal"
                 else "Evolution setup error" if args.command == "propose-prompt"
                 else "Execution setup error" if args.command == "execute-task"
                 else "Benchmark/scoring error" if args.command in {"inspect-benchmark", "score-execution"}
                 else "Storage initialization error" if args.command == "init-storage"
                 else "Saved-run summary error" if args.command == "summarize-run" else "Plugin assembly error")
        print(f"{label} [{error.code}]: {error}", file=sys.stderr)
        return 2
    except (OSError, UnicodeError, ValueError, KeyError) as error:
        print(f"Cannot load scoring input: {error}", file=sys.stderr)
        return 2
    output = json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    if args.command == "run-task" and args.output is None and not args.json:
        print(f"运行日志：{payload['log_path']}")
        if payload.get("round_records_path"):
            print(f"分轮记录：{payload['round_records_path']}")
        print(f"完整结果：{FileRoot(payload['run_root']).path(payload['record_ref']['uri'])}")
    elif args.output is None:
        # In-process callers may replace stdout with a restricted custom stream.
        try:
            output.encode(sys.stdout.encoding or "utf-8")
        except UnicodeEncodeError:
            output = json.dumps(payload, ensure_ascii=True, indent=2, allow_nan=False) + "\n"
        sys.stdout.write(output)
    else:
        try:
            input_paths = {loaded.source_path}
            from .environment import env_file_paths
            input_paths.update(env_file_paths(loaded))
            if loaded.benchmark_index_path is not None:
                input_paths.add(loaded.benchmark_index_path)
            if loaded.config.agent.initial_snapshot_ref is not None:
                input_paths.add(Path(loaded.config.agent.initial_snapshot_ref))
            if args.command in {"inspect-benchmark", "score-execution", "execute-task", "propose-prompt", "validate-proposal", "run-task"}:
                input_paths.update(prepared_benchmark.benchmark.source_paths)
                if args.command == "run-task" and args.output.resolve().is_relative_to(orchestration_setup.execution.storage.run_root.resolve()):
                    raise ValueError("output must be outside the managed run directory")
                if args.command == "validate-proposal":
                    input_paths.add(args.proposal.resolve())
                    if args.output.resolve().is_relative_to(validation_setup.execution.storage.run_root.resolve()):
                        raise ValueError("output must be outside the managed run directory")
                if args.command == "score-execution":
                    input_paths.add(args.execution.resolve())
                if args.command == "propose-prompt":
                    input_paths.add(args.record.resolve())
                    if args.output.resolve().is_relative_to(generation.execution.storage.run_root.resolve()):
                        raise ValueError("output must be outside the managed run directory")
                if args.command == "execute-task" and args.output.resolve().is_relative_to(execution_setup.storage.run_root.resolve()):
                    raise ValueError("output must be outside the managed run directory")
            if args.command == "init-storage" and args.output.resolve().is_relative_to(prepared.run_root.resolve()):
                raise ValueError("output must be outside the managed run directory")
            if args.output.resolve() in input_paths:
                raise ValueError("output must not overwrite a configuration, task index or Snapshot input file")
            args.output.write_text(output, encoding="utf-8")
            if args.command == "run-task" and not args.quiet:
                print(f"[结果文件] {args.output.resolve()}", file=sys.stderr, flush=True)
        except (OSError, UnicodeError, RuntimeError, ValueError) as error:
            print(f"Cannot write resolved configuration to {args.output}: {error}", file=sys.stderr)
            return 2
    if args.command == "execute-task" and (task_record.execution.status in {"error", "budget_interrupted"} or task_record.assessment_error is not None):
        return 2
    if args.command == "propose-prompt" and proposal.response is not None and proposal.response.status in {"error", "budget_interrupted"}:
        return 2
    if args.command == "validate-proposal" and any(report.status != "complete" for report in validation_result.reports):
        return 2
    if args.command == "run-task" and run_result.status in {"error", "budget_interrupted"}:
        return 2
    return 0
