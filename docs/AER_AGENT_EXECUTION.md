# Agent 执行层与轻量诊断

步骤 8 已实现本地 ModelProvider、ToolProvider、ReAct Agent、Single-call Agent 和轻量 Diagnoser。`execute-task` 可以从 Snapshot 执行一个任务、外部评分并保存结果；开发任务另有公开证据诊断。步骤 9 已实现 [路由与 Prompt 候选生成](AER_PROMPT_EVOLUTION.md)，配对验证及完整 Runner 仍待实现。

## 使用方法

执行 ReAct Agent：

```powershell
.\.venv\Scripts\aer.exe execute-task experiments/configs/v0_1_no_evolution.yaml --task-id dev-001
```

执行第二个 Agent：

```powershell
.\.venv\Scripts\aer.exe execute-task experiments/configs/v0_1_single_call.yaml --task-id dev-001
```

可指定 `--run-id demo-react` 重开同一配置的运行目录，或用 `--output execution-report.json` 保存 JSON 输出。输出不能覆盖输入配置、Benchmark 数据或运行目录中的托管文件。答错、格式错误和步骤上限为正常任务结果，退出码 0；执行或评估基础设施错误返回错误记录和退出码 2；setup 错误输出 stderr，退出码 2。

`execute-task` 的范围始终是一次独立执行与评估。即使传入 fixed/manual 配置，也不实例化 Router 或 Evolver，不创建 EvolutionAttempt，不提交版本。最终测试要求禁用进化并导入冻结 Snapshot；测试评分不传给 Diagnoser，不写入进化经验。

## 两个 Agent 与 Snapshot

| Agent | 模型调用 | 工具 | 步骤限制 |
| --- | --- | --- | --- |
| react_agent | 反复请求，直到最终输出或达到上限 | 支持 calculator，单轮多个调用顺序执行 | Snapshot 的 max_steps |
| single_call_agent | 一次请求 | 不提供工具定义，也不派发工具 | 必须为 1 |

两者实现相同的 AgentPlugin。模型、工具和工件存储通过工厂依赖注入，不在 Agent 内实例化具体 Provider。每次 run 新建消息列表、轨迹和成本记录，初始消息为 Snapshot Prompt 与 TaskView JSON；不复用上一次任务的观察或会话。

传入 Snapshot 是本次执行依据：Prompt 从其工件引用读取，max_steps 从其配置读取；模型和工具配置须与注入的运行依赖配置兼容。run 不改写 Agent 保存的 Snapshot 或稳定指针；restore 明确切换受控 Snapshot，snapshot 返回该受控状态。两者都不保存聊天会话。

max_steps 计模型交互轮数，工具调用不额外占模型轮；最后一轮中的工具仍执行并记账，但没有下一轮额度时返回 step_limit。它与 max_evolution_rounds 无关。

模型最终响应必须是完整 JSON 对象。解释文本、Markdown 围栏、数组、重复 JSON key、非有限数值和非法对象键都不会被自动修复。解析失败保留原始响应与 output_parse_error，返回空 output，由 Evaluator 判定格式错误；字段类型错误则直接交给 Evaluator 检查。Provider/工具基础设施异常返回 status=error，并保留已有调用成本。

## 本地 Provider

scripted_model 的场景为 mvp_v0_1_contract，型号为 scripted-v0.1。它从公开输入计算、提取或格式化响应，不读取评分规格、不查询任务 ID 答案。提供 calculator 时先发起工具请求，再根据观察生成最终对象；不提供工具时直接进行本地计算。状态由请求消息决定，不依赖共享调用游标。

这是工程验收的确定性替身，没有调用真实 LLM，也不用于研究性能结论。模拟 tokens 使用字符数除以 4 向上取整：输入基于请求 JSON，输出基于响应内容或工具调用 JSON。usage_source=simulated；输出受 request/run_ctx 中较小的 max_output_tokens 限制，超限返回 length 和截断响应。实际模型可切换为 [Qwen Provider](AER_QWEN_PROVIDER.md)，从 .env 读取凭据并记录服务 usage。

deterministic_tools 公开 calculator schema。calculator 通过受限 AST 支持数值、括号、加减乘除和一元正负号；限制表达式长度、节点数、深度与结果大小，拒绝 Python 函数调用、属性访问和其他运算。整值结果归一化为 int，使数字输出可以满足公开 integer 格式要求。

工具输入错误、除零和不可用工具返回 ToolResult(status=error)，Agent 可以继续观察并恢复；基础设施超时抛出 OperationTimeout。工具 tokens 为已知 0，调用次数与耗时直接测量，usage_source=provider_reported。

本地模型和 calculator 协作检查配置的单次调用截止时间。Agent 在调用返回后也检查超时，保留已完成调用的成本；此检查不能强制中止一个不返回的第三方 Provider。新的 Provider 必须自行落实原生超时或可取消执行，当前没有后台线程取消机制。

## 轨迹与成本

TraceEvent.seq 从 0 递增，记录 model_request、model_response、tool_call、tool_result、output_parse_error、finish、step_limit 和 execution_error。模型和工具调用有独立 operation_id，工具请求和观察通过 call_id 关联；会话内重复工具 ID 或返回身份不匹配为契约错误。记录公开请求、响应、工具输入/结果、调用 usage 和 UTC 时间，不要求隐藏思维链。

ExecutionResult.usage 只汇总底层调用一次，不累加外层结果与内层调用。失败 Provider 报告的调用成本也保留；无 usage 的异常标记来源 unknown，模型 token 保持 null。该情况下 model_calls/tool_calls 表示已派发到 Provider 的尝试数，真实 SDK 的请求与重试次数应由其完整 usage 提供。总耗时是整个执行的单调时钟时间，不累加嵌套耗时。

完整预算管理继续暂缓：RunContext.budget 可以为 None，RunContextRecord.remaining_budget 对应为 null，不用假的无限额度。tokens、工具次数和 episode 时间硬上限当前不生效，单次超时、输出上限和 Agent max_steps 已生效。

## 诊断器

Diagnoser 仅接收 TaskView、ExecutionView、FeedbackView 和 RunContext，不读私有答案，不返回 target/operator。

公开反馈由 PublicFeedbackBuilder 统一构建，独立执行与完整进化闭环共用相同逻辑。Benchmark.feedback_policy 声明任务输出和元数据的公开字段、静态反馈模板及进化权限；ToolSpec 声明工具参数和结果的反馈白名单；AgentCapabilities 声明可见状态与配置。通用启动流程不解析 ToyInput，不要求 output_format.fields，也不识别 calculator 字段。Qwen 工具请求只发送 name、description、parameters，反馈权限声明不会混入服务端工具参数。

Qwen 示例采用 `llm_diagnoser`，使用启动层注入的同一个 ModelProvider。失败时进行一次模型调用，要求返回 reason_code、explanation、observations、hypotheses、evidence_step_ids 和可选 confidence；事实和假设分开，涉及轨迹的证据必须引用实际可见的事件序号。输出为严格 JSON，不允许工具调用或 target 字段；单次超时和输出上限来自 RunContext。外部评分成功时直接返回 no_failure，不额外调用模型。最终测试不进行诊断。

模型诊断的输入为过滤后的公开任务、执行轨迹和固定公开评分反馈，排除 SDK 原始请求、私有评分自由文本和隐藏评分规格。诊断正文标记 source=model，正常及失败的模型调用都保存 DiagnosisRecord 工件，包含实际输入视图、原始模型响应、诊断结果/错误和 usage。模型响应 JSON 不合法、引用不存在的事件、达到输出上限或调用失败时，记录 DiagnosisError 及实际已消耗成本。

规则插件 `lightweight_diagnoser` 继续用于本地示例和可替换测试，按以下顺序匹配：

| 优先级 | 标签 | 条件 |
| ---: | --- | --- |
| 1 | no_failure | 外部评分成功，含工具错误后恢复 |
| 2 | step_limit | 有步骤上限事件 |
| 3 | invalid_output_format | 外部反馈指出格式错误 |
| 4 | tool_error | 执行状态明确标记因工具错误终止 |
| 5 | incorrect_answer | 格式有效，外部评分答案错误 |
| 6 | unknown_failure | 公开证据不足 |

证据引用真实 TraceEvent.seq；confidence 为 null。答案错误且观察到工具错误时，解释只记录该事实，不宣称两者有因果关系。评分基础设施失败不会被伪造为 0 分后送入诊断；执行基础设施失败也不构造假评分。诊断异常保留真实评分，并在 assessment_error 记录错误。

## 输出与持久化

TaskExecutionRecord 是核心可序列化记录，包含 run_id、episode_id、任务身份、ExecutionResult、EvaluationResult、FeedbackView、Diagnosis、assessment_usage 和可选 assessment_error。execution.usage 仅包含 Agent 执行消耗，assessment_usage 包含诊断消耗；execute-task 输出顶层 usage 是两者之和。验证评分与执行的任务/版本/operation 关联，以及包括事实/假设在内的诊断证据确实存在。

命令输出的主要结构：

```json
{
  "status": "executed",
  "run_root": "...",
  "dataset_version": "toy.v0.1.0",
  "scorer_version": "toy.scoring.v1",
  "record_ref": {"artifact_id": "...", "kind": "task_execution", "version": "...", "uri": "...", "digest": "..."},
  "record": {
    "run_id": "...",
    "episode_id": "...",
    "task": {"benchmark_id": "toy_tasks", "task_id": "dev-001"},
    "execution": {},
    "evaluation": {},
    "feedback": {},
    "diagnosis": {},
    "assessment_error": null
  }
}
```

上例省略各结果内部字段。完整记录作为不可变 task_execution 工件保存到本地 Artifact Store。run_manifest.json 的 execution 分区保存实际执行插件、Benchmark/评分版本、任务选择和模型参数，storage 分区记录存储初始化与完整实验配置。重复执行生成新的 episode 与记录，不覆盖稳定工件。

Python 调用：

```python
from experiments.execution import prepare_execution

prepared = prepare_execution("experiments/configs/v0_1_no_evolution.yaml")
record = prepared.run_task("dev-001")
print(record.execution.output, record.evaluation.score, record.diagnosis.reason_code)
payload = prepared.save_record(record)
```

后续 Orchestrator 可以直接使用 AgentPlugin、EvaluatorPlugin、DiagnoserPlugin 接口组织进化闭环。execute-task 当前不会产出完整 RunResult 或声称实验已完成。
