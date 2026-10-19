# AER v0.1 领域模型与同步接口

开发步骤 3 已实现。此文说明当前代码中的类型和接口；配置、轮数和接受阈值仍遵循 [实验语义](AER_V0_1_SPEC.md)。步骤 4–5 已实现 [注册与装配](AER_PLUGIN_ASSEMBLY.md) 和 [本地存储](AER_LOCAL_STORAGE.md)，步骤 7–11 已实现评分、执行、诊断、生成、[配对验证](AER_PAIRED_VALIDATION.md) 与 [主控制循环](AER_ORCHESTRATOR.md)。预算账本和批量 Runner 仍待实现。

## 1. 模型位置与数据边界

| 模块 | 主要类型 |
| --- | --- |
| `aer_core/schemas/task.py` | Task、TaskIdentity、TaskView、EvaluationSpec |
| `aer_core/schemas/feedback.py` | TaskFeedbackPolicy：任务公开反馈声明；ContextPolicy：组合后的反馈过滤策略 |
| `aer_core/schemas/references.py` | ArtifactRef、RecordRef、ManifestRef、AgentSnapshot |
| `aer_core/schemas/execution.py` | TraceEvent、ExecutionResult、EvaluationResult、Diagnosis、DiagnosisStatement、ExecutionView、FeedbackView、ErrorInfo |
| `aer_core/schemas/diagnosis.py` | DiagnosisRecord：公开诊断输入、模型响应、分析结果/异常、时间和实际 usage |
| `aer_core/schemas/assessment.py` | TaskExecutionRecord：关联独立执行、外部评分、公开反馈和诊断 |
| `aer_core/schemas/usage.py` | UsageReport、ResourceUsage、Reservation、BudgetRemaining |
| `aer_core/schemas/runtime.py` | RunContext、RunContextRecord |
| `aer_core/schemas/evolution.py` | 能力声明、EvolutionContext、EvolutionDecision、EvolutionRequest/Record、EvolutionResponse、EvolutionCandidate、ExperienceSummary |
| `aer_core/schemas/proposal.py` | EvolutionProposalRecord：一次路由与生成尝试或提前停止 |
| `aer_core/schemas/validation.py` | PairSeed、ValidationPlan、ValidationMetrics、LocalValidationMetrics、ValidationReport、AcceptanceResult |
| `aer_core/schemas/paired.py` | ScoredExecution、PairedTaskResult、ValidationEvidence、ValidationBatchResult |
| `aer_core/schemas/experience.py` | EvolutionAttempt、CandidateEvaluation、CommitRequest、VersionCommitRecord、ExperienceQuery、五类经验事件 |
| `aer_core/schemas/experiment.py` | RunResult、ExperimentResult |
| `aer_core/schemas/providers.py` | SDK 无关的模型消息、请求、响应、工具定义、调用与结果 |
| `aer_core/schemas/states.py` | RunStage、RunStatus、ExecutionStatus、AttemptStatus、CandidateStatus、ValidationStatus、StopReason |

ModelResponse 新增可选 metadata（默认空对象），用于记录服务返回的响应 ID、实际模型标识与 token 细分，不携带 API 密钥或隐藏 reasoning_content。旧响应记录保持可加载。

公开接口直接使用这些类型；开放的任务输入、工具参数、消息载荷、评分方法参数和扩展 metadata 仍使用受 JSON 类型约束的对象，其外层语义不再依赖未声明的 dict key。

DomainModel 使用严格类型、禁止多余字段和冻结字段。JSON 对象递归转换为只读 Mapping，JSON 数组与模型集合使用 tuple；输出 JSON 时恢复对象和数组。Snapshot 不持有客户端、会话或预算对象。

构造时复制嵌套数据，后续修改原始输入字典不会改变 Snapshot。使用 `replace(**updates)` 创建经校验的新值；`model_construct` 与 `model_copy(update=...)` 会绕过 Pydantic 校验，不用于生成领域状态。配置类沿用步骤 2 的实现。

TaskView、ExecutionView、FeedbackView、ExperienceSummary 是公开数据边界；EvaluationSpec 和完整评估证据属于评估侧。类型阻止传入额外的私有评估字段；公开文本和 metadata 的实际允许字段筛选由后续 ContextBuilder 完成，不能以一次通用 model_dump 代替反馈筛选。

## 2. 序列化与实时对象

领域记录使用 `model_dump_json()` 保存，使用 `model_validate_json()` 恢复。JSON dict 可以通过 `model_validate()` 恢复，JSON Schema 可以通过 `model_json_schema()` 获得。UTC 时间接受带时区的 datetime 或 ISO 字符串，保存时归一化到 UTC；不接受无时区时间。

```python
from aer_core.schemas.task import Task

task = Task(
    task_id="dev-001",
    benchmark_id="toy_tasks",
    input={"question": "1 + 1"},
)
restored = Task.model_validate_json(task.model_dump_json())
assert restored == task
```

RunContext 是冻结的运行时 dataclass，可持有实现 BudgetHandle 的实时对象；步骤 8 在预算暂缓时允许 None，记录中的 remaining_budget 对应 null。EvolutionRequest 同样持有实时 RunContext。保存这两者时使用 `to_record()`，得到 RunContextRecord/EvolutionRequestRecord，再进行 JSON 序列化：

```python
record_json = run_context.to_record().model_dump_json()
request_json = evolution_request.to_record().model_dump_json()
```

恢复运行时对象时，由调用者显式重新注入 live BudgetHandle 或 RunContext。记录中的 remaining_budget 是观察快照，`from_record()` 不据此覆盖预算账本。没有额外序列化任意 Python 对象或 SDK 客户端的机制。

## 3. 关联与一致性约束

TaskIdentity 使用 benchmark_id、task_id 两个字段表示任务身份。评估记录新增 evaluation_id、task_id、agent_version_id、execution_operation_id、operation_id，以绑定对应的执行。ValidationReport 新增 validation_id，供 CandidateEvaluation.validation_ref 关联。

ArtifactRef 标识不可变工件；RecordRef 用 record_id、uri 标识结构化记录；ManifestRef 用同样结构标识运行 manifest。URI 是否存在、工件内容与摘要是否匹配由未来 Store 检查，schema 构造不访问文件。

模型已校验以下跨字段关系：

- TraceEvent.seq 严格递增，反馈和诊断证据指向可见事件。
- EvolutionContext 的任务与稳定版本匹配其执行，历史时间不晚于 as_of。
- EvolutionRequest 决策属于可行集合，并有对应稳定工件。
- 候选 ID 不重复；artifact_updates 是通用非空工件映射，key 匹配 kind，具体更新范围由 Agent/Evolver 能力约束。
- ValidationPlan 携带 current_task，旧版执行/评分同时提供或同时为 null；独立两组任务非空、互斥，每个独立任务恰好一个配对 seed。
- 验证总体分数按任务数加权；完整报告包含 Local/独立证据、旧版/候选指标和已知候选用量。
- 首版 reward 等于独立验证 delta_score；未知收益和对应 delta 为 null。
- CandidateEvaluation 的接受、选择、提交标记与状态相符；commit_error 表示已选中但未提交。
- CommitRequest 的父版本匹配预期稳定版本，接受结果对应同一候选。
- RunResult 的最终评分匹配最终稳定版本及执行操作；未评估的新版本不能使用旧分数。已评估最终版本时，last_evaluated_* 与最终结果一致；否则可保留上一版本的最近评分。

步骤 5 的本地 Store 已落实版本/工件引用解析、候选与已保存验证报告关联、原子指针切换和恢复凭据；模型校验已有字段关系。步骤 7 已实现独立的确定性任务评分，并为 EvaluationResult 增加可选公开 reason_code，旧记录默认 null。步骤 10–11 已接入配对执行、接受策略及主控制循环。

## 4. 同步 Protocol

所有接口都在 `aer_core/contracts/`，只依赖核心配置与领域类型。

| 接口 | 公共方法 |
| --- | --- |
| AgentPlugin | capabilities()；run(TaskView, AgentSnapshot, RunContext) → ExecutionResult；snapshot()；restore(AgentSnapshot) |
| BenchmarkPlugin | tasks(split)；task_view(Task)；evaluation_spec(Task)；index()；feedback_policy(Task) → TaskFeedbackPolicy；声明 dataset_version、source_paths |
| EvaluatorPlugin | evaluate(Task, ExecutionResult, EvaluationSpec, RunContext) → EvaluationResult；声明 scorer_version、score_policy |
| DiagnoserPlugin | diagnose(TaskView, ExecutionView, FeedbackView, RunContext) → Diagnosis |
| RouterPlugin | select(EvolutionContext) → EvolutionDecision |
| EvolutionPlugin | capabilities()；evolve(EvolutionRequest) → EvolutionResponse |
| ValidationPlugin | validate(stable, candidates, ValidationPlan, RunContext) → tuple[ValidationReport, ...] |
| AcceptancePolicy | evaluate(ValidationReport) → AcceptanceResult，阈值由插件装配配置 |
| ArtifactStore | put(bytes, kind) → ArtifactRef；get(ArtifactRef) → bytes |
| VersionStore | save/load；initialize/stable；materialize；save_validation/load_validation；commit(CommitRequest) → VersionCommitRecord |
| ExperienceStore | append(ExperienceEvent) → RecordRef；get(RecordRef)；events()；query(ExperienceQuery) → tuple[ExperienceEvent, ...] |
| ExperimentRunner | setup(ExperimentConfig)；run_task(Task) → ExperimentResult；reset(scope)；close() |
| BudgetHandle | remaining()；reserve(operation_id, stage, tokens, tool_calls)；settle(operation_id, UsageReport) |
| ModelProvider | generate(ModelRequest, RunContext) → ModelResponse |
| ToolProvider | specifications() → tuple[ToolSpec, ...]；invoke(ToolCall, RunContext) → ToolResult |

Agent 和 Diagnoser 接收公开 TaskView；Evaluator 接收私有评分规格。Router 保持 select(context) 契约，Operator 由 Resolver 指定。ValidationPlugin 只评估，AcceptancePolicy 只判定，VersionStore 才执行提交。

独立评分、任务执行和配对验证统一调用 EvaluatorPlugin.evaluate；通用代码不再调用某个评分器特有的 evaluate_record 或 feedback_view。EvaluationResult.usage 可选：本地无调用的评分器可省略；使用模型、工具或外部服务的评分器报告实际 ResourceUsage，成功及异常成本计入 ScoredExecution 一次，私有评分自由文本不进入公开反馈。返回值同时核对任务、版本、执行操作及评分操作。

成本归属分别为 ExecutionResult.usage（Agent）、EvaluationResult.usage（评分器）和 TaskExecutionRecord.assessment_usage（诊断）。ScoredExecution 汇总执行与评分，RunResult 再累计诊断、生成和验证；独立执行入口也按相同归属汇总，不重复累计嵌套记录。评分分数与 success 分开，标准接口支持 0–1 范围的连续分数，接受规则继续使用独立验证 delta_score。

TaskFeedbackPolicy 必须绑定具体 TaskIdentity，声明允许公开的输出字段、元数据字段、静态反馈模板以及是否允许构建进化上下文。权限来自 Benchmark，不依赖公开 metadata 中是否存在 split。ToolSpec 新增 feedback_arguments、feedback_outputs，默认不公开工具载荷；执行参数 Schema 不等于反馈权限声明。AgentCapabilities 声明 feedback_state_fields、feedback_config_fields，控制状态摘要和 Snapshot 配置投影。

PublicFeedbackBuilder 是独立执行、诊断和进化共用的过滤服务；ContextBuilder 负责在相同公开视图上加入稳定版本与历史摘要。原始请求、私有评分规格、评分自由文本、未声明的工具字段和其他工件不进入反馈。诊断证据必须属于过滤后的轨迹；评分失败时 no_failure 是契约错误，诊断调用的实际成本与凭据仍保留。

模块 docstring 记录了调用前条件、隔离、幂等预期和主要异常语义。`runtime_checkable` Protocol 的 isinstance 只能检查方法是否存在；签名、返回类型及业务语义仍需 Plugin Manager 与契约测试检查。

## 5. 状态与错误

RunStatus 包含 created/running 和 completed/error/budget_interrupted；RunResult 必须为终态。stop_reason 保持实验文档定义，错误停止、预算停止与正常结束分别对应相应终态。

EvolutionAttempt 使用 started/completed/empty_candidates/error/budget_interrupted。started 记录 Router 调用前状态；终态要求结束时间，启用预算时还要求结束预算，预算暂缓时两侧为 null。新增可选公开 diagnosis，旧记录默认 null。路由失败、空候选和生成失败都可表示，不要求候选一定存在。

EvolutionCandidate 的状态为 generated；候选评估状态使用 not_validated/accepted/rejected/incomplete/selected/committed/commit_failed。generated/validating 表示中间阶段，不能冒充 CandidateEvaluation 的完成记录。Orchestrator 调度状态转换并追加实际提交结果，RunResult 使用任务终态。

运行异常继承 AERError，代码位于 `aer_core/errors.py`。PluginError 下提供 UnsupportedCapability、ContractError、ExecutionError、EvaluationError、BenchmarkError、DiagnosisError、RoutingError、EvolutionError、ValidationError、CommitError、StorageError 和 OperationTimeout；BudgetExceeded 独立于 PluginError，表示预算停止。CommitFinalizationError 继承 CommitError，额外携带已经发布的 committed_record，区分普通提交失败与待补齐日志的真实提交。

异常通过 `to_info()` 得到可序列化 ErrorInfo，保留 code、stage、message、operation_id、plugin_id、evidence_ref 和已知 ResourceUsage。配置异常继续使用既有 ConfigError，不改变配置 CLI 的错误输出。

资源未知时 token 字段保持 null、usage_complete=false；ResourceUsage.zero() 仅表示已知没有消耗。异常不将未知收益或未知成本默认填为 0。

## 6. 经验事件与验收

ExperienceEvent 是按 record_type 区分的类型联合，支持 attempt_started、attempt_finished、candidate_evaluated、version_committed、task_finished。可使用 `pydantic.TypeAdapter(ExperienceEvent)` 解析 JSONL 单行，恢复具体事件类型。

测试包括 JSON/JSON dict 往返、Snapshot 不可变、引用匹配、预算未知值、状态一致性、未来经验隔离、五类事件恢复和异常成本保留。两个 Mock Agent、两个 Mock Router、两个 Mock Evolver 通过同一组接口调用验证可替换性；两个实际 Agent 已通过完整主循环中的接受、提交与复测验收。

Qwen 路由扩展保留 `RouterPlugin.select(context)`：EvolutionContext 可携带 RunContextRecord 与目标说明；运行上下文不发送给模型。EvolutionDecision 的可选 usage/record_ref 提供实际成本及调用工件引用。RoutingRecord 保存 ModelRequest、ModelResponse、决策、时间、usage 与错误，失败通过 RoutingError 关联工件。旧插件与旧领域记录可以继续缺省这些字段。
