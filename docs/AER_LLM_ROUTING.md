# Qwen 路由与执行步数进化

使用 `experiments/configs/v0_1_qwen_router.yaml`，由 `llm_router` 选择 `prompt` 或 `execution_limits`。模型、API 地址和密钥继续从根目录 `.env` 的 `QWEN_MODEL`、`QWEN_BASE_URL`、`QWEN_API_KEY` 加载。Agent、诊断器、Router 和 Prompt Evolver 通过同一个 ModelProvider 接口调用模型；步数 Evolver 不调用模型。

```powershell
.\.venv\Scripts\python.exe main.py run-task experiments/configs/v0_1_qwen_router.yaml --task-id hard-dev-001 --output router-result.json
```

示例保留现有 hard_local 任务与初始 8 步。初次任务成功时，沿用既有成功停止语义，不调用 Router。需要观察步数分支时，可以在独立调试配置中降低 `agent.max_steps`，而不是要求 Router 每次都选择步数进化。不同任务可能适合 Prompt、步数或都不能解决；本功能首先验证插件路由闭环。

## 配置

```yaml
evolution:
  mode: llm
  max_candidates_per_round: 1

routing:
  plugin: llm_router
  fixed_target: null
  manual_decisions: []

targets:
  allowed: [prompt, execution_limits]
  default_operators:
    prompt: simple_prompt_evolver
    execution_limits: step_limit_evolver
  operator_configs:
    execution_limits:
      step_increment: 2
      max_steps: 16
```

`agent.max_steps` 是初始值，`step_increment` 是每轮增长值，Operator 的 `max_steps` 是增长上限。以上配置产生 8 → 10 → 12，后续能否继续还受进化轮数和接受结果影响。增长会截到上限；已经达到或超过上限时返回空候选，不降低步数。参数由插件配置模型校验，候选数量统一使用 `evolution.max_candidates_per_round`。Fixed/Manual Router 与旧 YAML 保持可用，也可以手动选择新目标。

## 路由模型的输入与输出

`plugins/routers/llm.py` 中的 `SYSTEM_PROMPT` 是系统提示词。用户消息包含 `operation=evolution_routing`、过滤后的任务/执行/评分反馈/诊断/历史、可行目标、已装配 Evolver 声明的目标说明，以及公开 Snapshot 工件的内容。

Router 通过 `EvolutionContext.run_context` 获取种子、超时和输出长度，保持 `select(context)` 接口。该运行上下文不发送给模型。隐藏评分规格、独立验证原始轨迹、凭据与工件路径不加入模型请求。诊断是输入证据，Router 自行选择目标，不要求诊断器推荐组件。

输出只允许如下结构，不允许模型自行指定 Operator、usage 或候选内容：

```json
{"target": "execution_limits", "rationale": "执行已达到步数上限，仍需要后续模型调用形成最终答案。"}
```

非法 JSON、额外字段、空理由、不可行目标、工具调用、输出截断或调用超时会形成路由失败，保留实际成本与调用记录，不回退为 Fixed Router。

## 步数如何进入版本

ReAct 的初始 Snapshot 保存 `prompt` 和 `execution_limits` 两个工件。`execution_limits` 正文为 `{"max_steps":8}`。`step_limit_evolver` 仅生成该工件的新内容；不修改 Prompt、模型客户端或 Snapshot.config，也不持有 Version Store。

Agent 每次从传入 Snapshot 读取有效步数。旧 Snapshot 无此工件时，沿用其原有配置值；SingleCall Agent 固定一步，不支持该目标。`ExecutionResult.state_summary.effective_max_steps` 记录本次有效上限，`model_steps` 记录实际调用步数。

旧版验证读取旧工件，候选验证读取候选工件，其余外部运行条件和配对种子保持一致。缓存键包含完整 Snapshot。候选沿用现有接受策略，增加步数本身不构成接受理由；拒绝或验证不完整不会改变稳定版本。提交后直接采用候选在当前任务上的验证结果，后续 Prompt 进化保留已提交步数。相同版本/任务/seed/条件的 Agent 执行与评分只观察一次；Router、Diagnoser 和 Evolver 的调用独立计费。

## 日志与成本

`evolution.txt` 中的“Router 实际收到的提示词与返回结果（完整正文）”展开系统消息、用户消息、原始响应及调用成本，不截断提示词。每轮 `rounds/<任务与会话>/round-NNN/record.json` 的 `routing.model_call` 同样展开实际输入；用户消息中的 JSON 解析为对象，便于阅读。保存的原始调用工件保留逐字一致的 ModelRequest 和 ModelResponse。

路由失败时也保存实际请求和可获得的响应。调用工件放在当轮 `artifacts` 内，采用现有时间命名，不增加新的面向阅读的日志文件。旧报告不会被改写，旧运行也不会补造当时未记录的 Router 输入。

`EvolutionDecision.usage` 和 `record_ref` 是可选的兼容扩展。Fixed/Manual Router 无模型成本；模型 Router 报告实际成本，失败通过 RoutingError 携带 usage 和证据引用。`EvolutionAttempt.usage` 为路由与候选生成总消耗，`EvolutionResponse.usage` 为 Evolver 自身消耗，二者不重复累计进 RunResult。

主控制器、配对验证、接受策略和版本提交逻辑保持通用。启动层仅新增 Agent 初始工件回调、Operator 配置透传和模型路由的成本汇总，不按具体 Router/目标在主控制器中分支。
