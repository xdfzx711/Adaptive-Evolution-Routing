# Codex CLI Agent、提示词与模型／推理强度选择

`codex_cli_agent` 是任务执行插件，实现 AgentPlugin；主控制器没有 Codex 或模型名称分支。Codex 自己完成模型调用、工具选择和执行循环。Router、Diagnoser、Prompt Evolver、ModelSelectionEvolver 使用注入的框架 ModelProvider，示例中仍为 Qwen。执行模型变化不改变这些插件的模型。

可选的附加工具进化支持本地插件和已有 MCP 服务，配置与启动命令见 [工具选择进化](AER_TOOL_EVOLUTION.md)。原配置省略 tooling 时保持现有行为。

## 启动

先安装并配置 Codex CLI 0.162.1 或更新版本的认证，确保终端能运行 `codex --version`。Qwen 的 API、URL 和模型继续放在 `.env`。本次接入采用 CLI 的非交互协议，不安装另一套 Agent SDK。

默认跳过用户 config.toml，以避免旧 sandbox_mode 静默覆盖任务目录权限；仍使用本机 Codex 登录认证。需要读取用户配置中的 Provider 时，可用 `--set agent.config.ignore_user_config=false`，加载的配置应使用新的 permissions 约定，旧 sandbox_mode/sandbox_workspace_write 会产生兼容错误。可通过 agent.config.profile 选择独立 Profile。插件不修改原 Codex 配置。

```powershell
.\.venv\Scripts\python.exe main.py validate-config experiments/configs/v0_1_codex_router.yaml
.\.venv\Scripts\python.exe main.py run-task experiments/configs/v0_1_codex_router.yaml --task-id hard-dev-001 --output codex-result.json
```

默认执行模型为 `gpt-5.6-sol`，可选列表为 `gpt-6.1-sol`、`gpt-6-sol`、`gpt-6-luna`、`gpt-5.6-sol`。名称按实验配置保留；实际可用性取决于本机 Codex Provider 和账户。调用不支持的模型时记录执行错误，不自行回退。

命令行直接调整启动参数，不用修改代码或 YAML：

```powershell
.\.venv\Scripts\python.exe main.py run-task experiments/configs/v0_1_codex_router.yaml --task-id hard-dev-001 --set agent.config.initial_model=gpt-6-sol --set limits.max_evolution_rounds=5 --output codex-result.json
.\.venv\Scripts\python.exe main.py run-task experiments/configs/v0_1_codex_router.yaml --task-id hard-dev-001 --set 'agent.config.workspace_root=D:\aer-workspaces' --set agent.config.timeout_seconds=600.0
```

`--set` 接受点分路径和 YAML 值，支持字符串、数值、布尔值、null、列表与对象。覆盖在校验和装配前应用，所有后续加载使用同一份解析配置。禁用进化时同时设置 `evolution.mode=disabled`、`limits.max_evolution_rounds=0`、`routing.plugin=null`、`targets.allowed=[]`、`targets.default_operators={}`，以及空的 Operator 配置。

## 插件配置

`agent.config` 的字段：

| 字段 | 含义 |
| --- | --- |
| command | CLI 启动参数列表，默认 `[codex]`；Windows npm 安装通过 Node 启动其 JS 入口，不拼接 Shell 命令 |
| initial_model | 初始 model 工件的模型标识，默认 gpt-5.6-sol |
| initial_reasoning_effort | 初始 model 工件的推理强度，默认 null，表示沿用 Codex 后端默认值 |
| allowed_models | 本实验允许选择的模型，非空、不重复，必须包含初始模型 |
| allowed_reasoning_efforts | 按模型配置允许的推理强度列表；缺省为 `{}`，未列出的模型仅允许 null（后端默认） |
| workspace_root | 绝对路径；默认系统临时目录下 aer-codex-workspaces，必须在 AER 项目之外 |
| timeout_seconds | 一次完整 CLI 执行的超时，默认 300 秒，超时终止进程树 |
| profile | 可选 Codex Profile 名称 |
| ignore_user_config | 是否跳过用户 config.toml，默认 true；认证仍由 Codex 管理。显式继承配置时，旧 sandbox_mode/sandbox_workspace_write 会触发清楚的兼容错误，避免覆盖任务隔离规则 |
| windows_sandbox | Windows 后端，默认 mxc；可显式配置 elevated。unelevated 无法执行本插件的读取隔离规则，因此不允许选择 |
| startup_retries | 默认 2 次；只重试尚未创建线程的 MXC CreateProcessSecurityEnvironment 0x80070003 启动失败。设为 0 可关闭；重试共享一次执行的 timeout_seconds，不重试已经开始的任务 |

Windows 下插件显式传递 windows.sandbox=mxc。独立 CLI 的默认后端可能是 unelevated，该后端无法执行分离的文件读取限制；这会在创建会话、读取 AGENTS.md 时失败，尚未进入模型调用。如果设备不支持 MXC，可在已完成相应系统设置的环境中显式使用 --set agent.config.windows_sandbox=elevated，不会自动切换为无隔离执行。该设置进入 Snapshot 与评测条件。

MXC 的本地命令检查不覆盖完整会话初始化。针对 0x80070003 的启动重试要求 stdout 为空或只有错误事件；一旦观察到 thread.started、turn.started、任务事件或无法解析的输出，立即停止自动重试。重试写入执行轨迹的 startup_retry，实际次数保存在 state_summary.startup_attempts。模型列表刷新超时不单独触发此机制，权限配置也不会在重试时变化。

权限规则只保留最外层受保护目录。主控制器的 RunContext.workspace_ref 是逻辑路径，可能尚未创建多层父目录；在本机 MXC 上，将这种路径再次列为项目目录内的 deny 条目会导致会话初始化报 0x80070003。项目目录的 deny 已覆盖其所有后代，因此插件省略重复子路径；项目外的运行目录仍保留独立 deny，不创建占位目录。完整 CLI 回归覆盖这种正式运行的路径结构，并关闭重试，确保验证的是首次启动成功。

Codex 不接受 `agent.max_steps`。ReAct 的执行上限仍属于其自身配置，不再是进化目标。框架的 Qwen 调用超时和输出上限不冒充 Codex 内部每次调用的上限。

## 版本与候选

Windows 任务输入附带 execution_environment：实际工作区、原生命令路径和绝对路径使用说明。插件对检测到的独立 Git/rg 安装目录仅授予读取权限，继续禁止访问项目、评分数据及运行记录；不依赖受限 Shell 的 PATH 或 PowerShell 当前目录。运行时命令路径进入评测条件记录。

CLI 的 error / turn.failed 标准输出事件会保留在最终异常中，即使 stderr 为空也能看到额度耗尽等实际原因。已启动的任务仍不自动重试。

执行层持续读取 stdout/stderr。收到 turn.completed 或 turn.failed 后给予 2 秒退出宽限时间；CLI 未退出时结束本次进程树，记录 shutdown_after_terminal_event。完整成功事件、最终输出和 usage 仍按原契约校验；未收到终止事件时仍执行原超时限制，自然的非零退出仍报错。这避免将已完成但退出停滞的 CLI 会话误报为任务执行超时。

Snapshot.artifacts 保存 `prompt` 和 `model`。prompt 是可进化的附加 developer_instructions；model 同时保存模型与推理强度，例如：

```json
{
  "model_id": "gpt-5.6-sol",
  "reasoning_effort": "high"
}
```

Codex 将前者映射为 `--model`，后者映射为 `--config model_reasoning_effort="high"`。运行配置和候选组合列表由 Snapshot.config 冻结，initial_model／initial_reasoning_effort 只负责创建初始工件，后续每次执行都读取传入 Snapshot 中的 model 工件。

旧配置、旧 Snapshot 以及仅含 model_id 的工件继续可用。省略 reasoning_effort 或设为 null 时不向 CLI 传递该覆盖项，不自动指定 medium。未配置 allowed_reasoning_efforts 时，原命令仍只进化模型。启用联合进化时，在 agent.config 下配置，例如：

```yaml
initial_model: gpt-5.6-sol
initial_reasoning_effort: medium
allowed_models: [gpt-6.1-sol, gpt-6-sol, gpt-6-luna, gpt-5.6-sol]
allowed_reasoning_efforts:
  gpt-6.1-sol: [low, medium, high]
  gpt-6-sol: [low, medium, high]
  gpt-6-luna: [low, medium, high]
  gpt-5.6-sol: [low, medium, high]
```

档位列表按实验所用 Provider 实际支持的组合填写；配置允许 low、medium、high、xhigh、max、ultra 和 null，具体模型不一定支持全部档位。列表必须非空、不重复，模型键必须属于 allowed_models，初始组合也必须在列表中。如需让 Router 选择恢复后端默认，应将 null 加入对应列表。Provider 拒绝某组合时记录执行错误，不自动降档或切换模型。

也可只在命令行开启，不修改原 YAML。例如沿用已有 Jev 三题配置：

```powershell
.\.venv\Scripts\python.exe main.py run-task experiments/configs/v0_1_codex_swebench_jev.yaml --task-id psf__requests-1963 --set agent.config.initial_reasoning_effort=medium --set 'agent.config.allowed_reasoning_efforts={gpt-6.1-sol: [low, medium, high], gpt-6-sol: [low, medium, high], gpt-6-luna: [low, medium, high], gpt-5.6-sol: [low, medium, high]}' --output model-reasoning-result.json
```

任务 ID 应使用该配置实际包含的实例；可在原来的 run-task 命令中追加相同的两个 --set 参数。此处仅覆盖 Agent 配置，不覆盖 Jev／Qwen 的依赖配置。

Prompt Evolver 只更新 prompt；ModelSelectionEvolver 根据允许反馈，用注入的 Qwen Provider 从当前组合之外的列表中选择完整配置，只更新 model。它允许仅换推理强度、仅换模型或同时改变两者。返回完全相同的组合或 model_id=null 时为空候选，越界组合报错。即使只有一个允许模型，只要存在另一推理强度，model 目标仍可行；仅剩当前组合时不可行。每轮默认一个候选，接口保留多候选能力。兼容旧的仅含 model_id 的模型响应，此时保留当前推理强度；如果新模型不允许该强度，拒绝该响应。

通用选择类型与 ExecutionModelAdapter 协议位于 plugins/model_selection.py；CodexModelAdapter 位于 plugins/agents/model_selection.py，负责解释本插件的候选空间、读取和编码工件。model_selection_evolver 的工厂显式注入该适配器，Evolver 本身不依赖 Codex 模型名。接入其他任务 Agent 时，实现其适配器和执行参数映射，并在插件注册中声明兼容关系；可复用同一 Evolver，无须修改主控制器。当前未给 ReAct／SingleCall Agent 新增 model 能力。

候选从当前稳定版本继承其余工件。配对验证使用相同公开任务、初始环境和评分规则；接受策略沿用现有分数提升与回归保护。成本单独记录，不将低成本等同于接受理由。拒绝或验证失败不修改稳定指针；提交后复用候选的当前任务验证结果，不重新执行。

## 工作区与输出

每次实际执行创建独立目录和新会话，使用 `--ephemeral`，不 resume。任务通过 stdin 输入；工作区只生成公开输出 Schema，Codex 产生的文件保留在该工作区。CLI 被配置为无需交互审批、限制文件访问，并拒绝读取 AER 源码目录和当前 run 目录，避免工作区外的评分数据与历史报告进入执行。CLI 配置或受管策略不兼容时记录错误，不自动放宽权限。隔离的实际执行效果仍需在目标系统进行真实 CLI 联调。

PublicJsonOutput 根据公开 output_schema 或 output_format 生成约束，并严格解析最终 JSON 对象，交给原 Evaluator；不读取隐藏答案，不修补答案，不从自由文本中猜测结果。Codex 事件转换为通用 model_response/tool_call/tool_result/finish 等 TraceEvent。Agent 声明允许披露的工具参数与输出字段，供诊断、路由和进化使用。

CLI 版本和用户配置文件摘要进入评测条件；模型工件、提示词和完整 Snapshot 进入缓存身份。CLI 不暴露可控采样 seed，日志明确记录 seed_supported=false；配对种子仅作为 AER 运行关联条件。

## 成本、日志与测试

turn.completed 的 input_tokens/output_tokens 作为实际用量；cached_input_tokens 已包含在输入总数中，不重复相加。未报告 token 时保持未知，不能进入完整验证缓存。CLI 内部模型请求次数不可见，model_calls=null；工具次数仅统计可见 CLI 工具事件。已知 token 不会因为请求次数未知而变成模拟计费。

沿用 evolution.txt、每轮 record.json 和工件/版本存储，不增加平行日志。执行记录包含实际附加指令、公开输入、执行模型与所选推理强度、CLI 版本、轨迹、工作区与成本；Router 提示词继续完整记录。后端默认值未显式指定时记录 null，不推测其实际档位。

离线测试通过独立 Python 子进程模拟 CLI，覆盖模型与 Prompt 继承、评分、接受、拒绝、空候选、验证失败、CLI 失败、超时、成本、缓存、提交后复用和命令行覆盖。模拟测试不证明账户能够调用指定真实模型。

还可在 Windows 下设置 AER_TEST_REAL_CODEX=1，运行 tests.integration.test_codex_native。该测试启动实际 Codex CLI，并连接本地模拟 Responses 服务，覆盖完整会话初始化、模型参数传递、流式响应和输出解析；不会调用远端模型服务。启用该检查可发现普通假 CLI 和单条沙箱命令无法覆盖的原生启动问题。

官方协议：[非交互模式](https://learn.chatgpt.com/docs/non-interactive-mode)、[CLI 参数](https://learn.chatgpt.com/docs/cli/reference)、[配置参考](https://learn.chatgpt.com/docs/config-file/config-reference)。
