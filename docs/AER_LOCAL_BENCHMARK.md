# 本地 Benchmark 与确定性评分

开发步骤 7 已实现 `toy_tasks` 和 `deterministic_evaluator`，可以独立加载真实任务并对已有执行记录评分。步骤 8–11 已加入 [Agent 执行与诊断](AER_AGENT_EXECUTION.md)、[配对验证](AER_PAIRED_VALIDATION.md) 和 [单任务主控制器](AER_ORCHESTRATOR.md)。另已新增独立的 [较难本地任务集 hard_local](AER_HARD_LOCAL_TASKS.md)，下文 4/8/4 划分专指原始 toy_tasks。完整预算管理和批量 Runner 按当前 MVP 范围推迟。

## 数据与划分

数据集版本为 `toy.v0.1.0`，评分规则版本为 `toy.scoring.v1`。任务顺序来自文件，不排序、不随机生成；修改任务或评分语义时更新对应版本。

| 划分 | 数量 | 任务 ID | 用途 |
| --- | ---: | --- | --- |
| dev | 4 | dev-001 至 dev-004 | 执行调试与进化触发 |
| validation / similar | 4 | validation-001 至 validation-004 | 相似任务迁移 |
| validation / regression | 4 | validation-005 至 validation-008 | 基础能力回归检查 |
| test | 4 | test-001 至 test-004 | 冻结版本最终评估 |

任务覆盖算术、文本字段提取和格式转换。算术任务用于观察 calculator 使用，但评分只检查最终答案与公开输出格式，不要求必须调用工具。小型任务集用于框架调试，不能据此推出研究收益。

`plugins/benchmarks/toy_tasks/tasks.json` 保存公开题目、来源、划分及输出要求；`scoring_specs.json` 独立保存正确答案和评分方法。两份文件随包发布，加载不依赖当前工作目录或网络。

每条任务公开输入具有以下结构：

```json
{
  "instruction": "Compute the expression and return the final JSON object.",
  "data": {"expression": "(17 + 8) * 3"},
  "output_format": {
    "fields": [{"name": "answer", "type": "integer"}],
    "allow_extra_fields": false
  },
  "numeric_tolerance": 0
}
```

`numeric_tolerance` 只用于数值任务，必须与私有评分规格一致。格式转换和提取任务使用结构化精确比较。格式要求公开；私有规格不能要求题目未声明的额外字段。

## Adapter 与数据传递

`ToyBenchmark` 实现 `BenchmarkPlugin.tasks(split)` 和 `evaluation_spec(task)`。加载时检查三个划分非空、任务与来源 ID 全局唯一、验证分组合法且均非空、评分规格一一对应、数据版本一致和评分规则有效。未知 split 抛出 `UnsupportedCapability`，数据错误抛出 `BenchmarkError`。

`task_view(task)` 从已加载的任务生成 Agent 输入，保留公开输入与元数据，移除 `eval_spec_ref`。不将 `EvaluationSpec` 传给 Agent、Router 或 Evolver。嵌套输入只读；返回 Task 不允许调用者修改内部内容。

`index()` 从真实任务生成现有 `BenchmarkIndex`。`prepare_benchmark()` 使用该索引校验 YAML 的实际任务选择和验证分组，按原始顺序过滤 `benchmark.task_ids`，返回固定的两组独立验证任务。测试 fixture 索引仅用于配置测试，不能代替这一步。

默认分组字段为 `validation_group`，值为 `similar`、`regression`。开启进化的配置指定其他字段或值时，必须与实际数据一致，否则 setup 失败。

最终测试沿用配置约束：禁用进化，并指定冻结的 `agent.initial_snapshot_ref`。本步骤只检查配置和任务划分，不实例化 Agent 或加载冻结版本内容；冻结版本内容与谱系由 `init-storage` 检查。

## 评分语义

Evaluator 接收规范化的 `ExecutionResult.output` 对象。后续 Agent 应负责解析最终 JSON；评分器不从解释文本中猜测答案，也不解析 `raw_text` 字段来补救格式。

| 方法 | 比较规则 |
| --- | --- |
| numeric_answer | `answer` 类型符合公开格式，且绝对误差不超过公开容差 |
| structured_answer | 所有公开必需字段类型正确，字段值精确匹配 |

`number` 接受有限整数或浮点数，`integer` 只接受整数；布尔值不能充当数字。字符串不自动转数字，大小写和空格不自动归一化。额外字段依据公开的 `allow_extra_fields` 处理。数值比较以十进制字符串表示转换为精确有理数，不受 Decimal 上下文精度影响。

格式与答案同时通过时 `score=1, success=true`，其他可评分输出为 `score=0, success=false`。指标为 `format_valid`、`answer_correct`；公开原因码为 `correct`、`invalid_output_format`、`incorrect_answer`。公开反馈不返回正确答案或私有评分参数。

`completed` 和 `step_limit` 执行可以评分；无法匹配的任务身份、无效评分规格或中断/错误执行抛出 `EvaluationError`，不会被当作普通 0 分。`EvaluationResult` 关联任务、Agent 版本、原执行 operation 和评分 operation，评分 ID 由这些身份及评分版本稳定派生。同一输出与规则产生相同分数，供 Harness 后续比较不同版本。

`EvaluationResult.reason_code` 是新增的可选字段，旧记录保持兼容；本评分器总会提供它。`feedback_view()` 生成不携带私有引用的公开反馈视图。独立验证的逐任务反馈仍由未来 Harness 留在评估侧，只向进化侧公开允许的聚合结果。

## 命令行与 Python

加载实际任务、检查划分并打印公开任务：

```powershell
.\.venv\Scripts\aer.exe inspect-benchmark experiments/configs/v0_1_fixed_prompt.yaml
```

对已有完整 ExecutionResult JSON 评分：

```powershell
.\.venv\Scripts\aer.exe score-execution experiments/configs/v0_1_fixed_prompt.yaml execution.json --operation-id score-v1
```

两条命令都支持 `--output`。结果正常为退出码 0，包括任务答错；配置、加载或评分错误为退出码 2。输出不能覆盖配置、执行输入或打包的数据集文件。`score-execution` 只接受配置所选执行任务，不会给其他划分任务评分。

Python 可独立使用 Adapter 和评分器，无需预算对象：

```python
from experiments.benchmark import prepare_benchmark
from aer_core.schemas.execution import ExecutionResult
from pathlib import Path

prepared = prepare_benchmark("experiments/configs/v0_1_fixed_prompt.yaml")
execution = ExecutionResult.model_validate_json(Path("execution.json").read_text(encoding="utf-8"))
evaluation = prepared.score(execution, operation_id="score-v1")
print(evaluation.model_dump(mode="json"))
```

后续控制器继续调用标准 `EvaluatorPlugin.evaluate(task, result, spec, run_ctx)`。独立命令使用 `evaluate_record`，只提供评分操作 ID，不伪造运行预算或执行成本。
