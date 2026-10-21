# Codex CLI Agent、提示词与模型选择

`codex_cli_agent` 是任务执行插件，实现 AgentPlugin；主控制器没有 Codex 或模型名称分支。Codex 自己完成模型调用、工具选择和执行循环。Router、Diagnoser、Prompt Evolver、ModelSelectionEvolver 使用注入的框架 ModelProvider，示例中仍为 Qwen。执行模型变化不改变这些插件的模型。

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
| allowed_models | 本实验允许选择的模型，非空、不重复，必须包含初始模型 |
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

Snapshot.artifacts 保存 `prompt` 和 `model`。prompt 是可进化的附加 developer_instructions；model 正文为 `{"model_id":"gpt-5.6-sol"}`，执行时唯一映射为 `--model`。运行配置和允许模型列表由 Snapshot.config 冻结，initial_model 只负责创建初始工件。

Prompt Evolver 只更新 prompt；ModelSelectionEvolver 根据允许反馈，用 Qwen 从当前模型之外的配置列表中选择一个模型，只更新 model。选择当前模型或 null 返回空候选；越界选择报错。只有一个允许模型时，model 目标不可行。每轮默认一个候选，接口保留多候选能力。

候选从当前稳定版本继承其余工件。配对验证使用相同公开任务、初始环境和评分规则；接受策略沿用现有分数提升与回归保护。成本单独记录，不将低成本等同于接受理由。拒绝或验证失败不修改稳定指针；提交后复用候选的当前任务验证结果，不重新执行。

## 工作区与输出

每次实际执行创建独立目录和新会话，使用 `--ephemeral`，不 resume。任务通过 stdin 输入；工作区只生成公开输出 Schema，Codex 产生的文件保留在该工作区。CLI 被配置为无需交互审批、限制文件访问，并拒绝读取 AER 源码目录和当前 run 目录，避免工作区外的评分数据与历史报告进入执行。CLI 配置或受管策略不兼容时记录错误，不自动放宽权限。隔离的实际执行效果仍需在目标系统进行真实 CLI 联调。

PublicJsonOutput 根据公开 output_schema 或 output_format 生成约束，并严格解析最终 JSON 对象，交给原 Evaluator；不读取隐藏答案，不修补答案，不从自由文本中猜测结果。Codex 事件转换为通用 model_response/tool_call/tool_result/finish 等 TraceEvent。Agent 声明允许披露的工具参数与输出字段，供诊断、路由和进化使用。

CLI 版本和用户配置文件摘要进入评测条件；模型工件、提示词和完整 Snapshot 进入缓存身份。CLI 不暴露可控采样 seed，日志明确记录 seed_supported=false；配对种子仅作为 AER 运行关联条件。

## 成本、日志与测试

turn.completed 的 input_tokens/output_tokens 作为实际用量；cached_input_tokens 已包含在输入总数中，不重复相加。未报告 token 时保持未知，不能进入完整验证缓存。CLI 内部模型请求次数不可见，model_calls=null；工具次数仅统计可见 CLI 工具事件。已知 token 不会因为请求次数未知而变成模拟计费。

沿用 evolution.txt、每轮 record.json 和工件/版本存储，不增加平行日志。执行记录包含实际附加指令、公开输入、执行模型、CLI 版本、轨迹、工作区与成本；Router 提示词继续完整记录。

离线测试通过独立 Python 子进程模拟 CLI，覆盖模型与 Prompt 继承、评分、接受、拒绝、空候选、验证失败、CLI 失败、超时、成本、缓存、提交后复用和命令行覆盖。模拟测试不证明账户能够调用指定真实模型。

还可在 Windows 下设置 AER_TEST_REAL_CODEX=1，运行 tests.integration.test_codex_native。该测试启动实际 Codex CLI，并连接本地模拟 Responses 服务，覆盖完整会话初始化、模型参数传递、流式响应和输出解析；不会调用远端模型服务。启用该检查可发现普通假 CLI 和单条沙箱命令无法覆盖的原生启动问题。

官方协议：[非交互模式](https://learn.chatgpt.com/docs/non-interactive-mode)、[CLI 参数](https://learn.chatgpt.com/docs/cli/reference)、[配置参考](https://learn.chatgpt.com/docs/config-file/config-reference)。
