# 上下文、目标路由与 Prompt 候选生成

步骤 9 已实现 ContextBuilder、TargetRegistry、OperatorResolver、Fixed/Manual Router 和 simple_prompt_evolver。现在可以从一份失败执行记录产生候选 Prompt，并保存生成尝试及未验证的候选结果。配对验证 Harness、接受策略执行与完整 Orchestrator 后续实现。

## 使用入口

先使用 fixed/manual 配置执行任务并保存报告，保持两条命令的配置及 run-id 一致：

```powershell
.\.venv\Scripts\aer.exe execute-task experiments/configs/v0_1_fixed_prompt.yaml --task-id dev-001 --run-id prompt-demo --output execution.json
.\.venv\Scripts\aer.exe propose-prompt experiments/configs/v0_1_fixed_prompt.yaml execution.json --run-id prompt-demo --round-id 1 --output proposal.json
```

初始评分成功时返回 task_succeeded，生成被跳过。脚本模型默认会答对当前任务集；工程调试可以使用测试里的步骤上限或错误响应 fixture。步骤上限 fixture 可以验证生成路径，不能作为改进收益证据。

使用 Qwen 的完整 fixed 配置为 experiments/configs/v0_1_qwen_fixed_prompt.yaml，仍从项目根目录 .env 读取 QWEN_API_KEY、QWEN_BASE_URL 和 QWEN_MODEL。执行与生成分别调用同一 Provider 接口，生成请求不提供工具。

propose-prompt 接收完整 TaskExecutionRecord 或 execute-task 导出的 JSON 报告；普通 ExecutionResult 缺少外部评分与 episode 关联，不能直接用于生成。--round-id 明确提供当前轮号，每个 episode 从 1 连续递增；路由错误、空候选和生成错误都会占用已开始的一轮。手动计划耗尽、达到轮数、进化关闭或没有可行目标在开始尝试前停止。

该命令只执行一次路由与候选生成，不反复运行 Agent，不验证或提交候选。正常跳过、空候选、候选生成成功为退出码 0；路由或生成异常保存错误记录并返回 2。输出不能覆盖执行报告、配置、.env 或托管运行目录。

## 公开上下文

ContextBuilder 输入来自已加载的公开 TaskView 和关联的 TaskExecutionRecord，核对任务、run/episode、稳定版本及外部评分。

| 部分 | 保留信息 | 权限处理 |
| --- | --- | --- |
| TaskView | Benchmark 已分离的公开题目和输出要求 | 根据绑定任务的 TaskFeedbackPolicy 过滤 metadata，按 allow_evolution 检查权限，不依赖输入格式或公开 split 字段 |
| 稳定 Snapshot | 版本/父版本、Agent ID、声明可见的配置与工件 | 根据 AgentCapabilities 生成公开投影，移除未声明配置与工件 |
| 执行 | 允许的输出字段、已知轨迹事件、成本及终止状态 | 丢弃未知事件、原始请求消息、模型 metadata 和未声明的工具字段 |
| 评分反馈 | score、success、公开原因码 | 使用固定公开解释，不复制评分器自由文本 |
| 诊断 | 允许标签、公开解释、事实、假设、有效证据步骤及 confidence | 模型诊断保留基于公开输入得到的分析；规则诊断使用固定公开说明；移除调用收据、usage 和不可见证据引用 |
| 历史 | 目标、诊断摘要、候选状态、聚合收益及接受/提交标志 | 不读私有验证工件、不复制策略理由自由文本 |

上下文中的 Snapshot 是决策用投影；实际执行与候选版本构造仍使用 Version Store 中的完整 Snapshot。Evolver 发给模型的上下文进一步移除 Snapshot 引用，单独加入 stable_version_id 和旧 Prompt 文本。

工具可见字段由 ContextPolicy 显式声明。当前 calculator 允许 expression 参数和 value 结果，错误仅保留发生事实；不会转发工具错误的任意消息、私有结果或证据引用。任务公开 input 的来源边界由 Benchmark Adapter 负责，ContextBuilder 不通过匹配答案字符串来猜测标签来源。

预算账本暂缓时 EvolutionContext.remaining_budget、EvolutionAttempt.budget_before/budget_after 都为 null。已有带预算的记录仍可加载；实际存在 BudgetHandle 时 ContextBuilder 可以读取剩余预算快照，首版 TargetRegistry 不实现资源预算过滤。

## 历史范围

independent 只使用同 run、同 episode、同任务的已完成较早轮次。continual 可加入同 run 先前已完成开发任务的尝试；其他 episode 必须有已完成任务记录，任务 ID 必须属于启动层提供的开发任务清单。

依据 as_of 截止时间检查事件时间和 payload 的完成时间，排除未来、未完成和同轮/后续尝试。空候选、异常和拒绝保留；没有评估收益时为 null，不填 0。历史理由使用状态码，历史诊断保留允许的公开分析，不附当前轨迹无法关联的旧证据步骤。EvolutionAttempt 新增可选 diagnosis，旧记录默认 null。

## 可行目标与路由

TargetRegistry 同时检查配置允许目标、Agent 支持目标、独立会话能力、已装配的配置 Operator、Agent/Operator 兼容性、Agent 支持的工件类型，以及 Snapshot 中所需工件。返回可行集合与每个目标的可用性原因；当前真实目标包括 prompt 和 execution_limits，后者仅适配 ReAct。Qwen 模型路由与步数进化见 [扩展说明](AER_LLM_ROUTING.md)。

OperatorResolver 根据 targets.default_operators 的精确映射选择实例，找不到或能力不匹配时报错，不自动替换。Fixed Router 只选配置目标；Manual Router 按 EvolutionContext.round_id 取人工计划，目标必须可行。两者输出 EvolutionDecision，不指定 Operator。

Manual 计划结束以 manual_plan_exhausted 停止，不复用最后一项或回退 Fixed；历史记录数量不影响本轮决策。EvolutionRequest 的 round_id 必须与上下文一致。独立旧上下文缺少 round_id 时默认 1，旧请求记录中的上下文从请求的明确 round_id 补齐。

## Prompt Evolver

simple_prompt_evolver 只注入 ModelProvider 和 ArtifactStore，支持 react_agent 与 single_call_agent。它读取旧 Prompt，结合过滤后的当前状态和历史请求完整的新 Prompt，要求可复用指令，不嵌入任务特定答案或验证/测试样例。

模型输出格式：

```json
{
  "candidates": [
    {"prompt": "Complete replacement prompt.", "rationale": "Reusable improvement based on public evidence."}
  ]
}
```

数量上限取请求与插件配置的较小值，默认每轮配置为 1；接口支持多候选。空数组或全部与旧 Prompt 相同返回 empty；重复内容去重。空白 Prompt、非法 JSON、额外结构字段、超过数量上限或模型工具调用为生成错误。先验证整批结构，再写新工件。

候选包含 candidate_id、parent_version_id、prompt 工件引用及修改说明，candidate_version_id 尚为空。Evolver 不持有 Version Store；新工件不可变，稳定指针和旧 Prompt 不变。模型成本包含空候选和生成失败，未知 tokens 保持 null；候选 metadata 可保留模型响应 ID 和实际模型标识。脚本模型的生成使用公开故障类别追加通用指令，只用于工程路径验收。

步骤 10 的 [配对验证入口](AER_PAIRED_VALIDATION.md) 会为这些候选构建 Snapshot 并进行接受选择。EvolutionCandidate 类型本身已使用通用 artifact_updates；本插件只输出 prompt 是它的能力约束，不是 Snapshot 构造的固定规则。

## 经验与输出

在 Router 调用前保存 AttemptStartedEvent，之后保存 AttemptFinishedEvent。生成出的每个候选保存 CandidateEvaluatedEvent(status=not_validated, acceptance=null, selected=false, committed=false)。这些记录表示生成阶段结束，不代表验证完成或获得收益。后续 Harness/Orchestrator 将负责验证与提交后的终态。

EvolutionProposalRecord 关联 run/episode/task/round、稳定版本、过滤上下文、尝试、路由决策、Operator、候选响应和经验引用。记录整体保存为不可变 evolution_proposal 工件；run_manifest.json 的 evolution 分区保存实际 Router 和 Evolver 的版本，具体配置保存在 storage 分区的 experiment_config 中。被跳过的生成保存 stop_reason，不创建新的尝试。

Python 用法：

```python
from experiments.evolution import prepare_evolution, load_task_record
from pathlib import Path

prepared = prepare_evolution("experiments/configs/v0_1_fixed_prompt.yaml", run_id="prompt-demo")
record = load_task_record(Path("execution.json"))
proposal = prepared.propose(record, round_id=1)
payload = prepared.save_record(proposal)
```

验收覆盖权限 canary、测试反馈隔离、独立/累计历史边界、不可行目标、人工计划耗尽、单/多候选、空候选、异常成本、稳定工件隔离和 JSON/CLI 往返。
