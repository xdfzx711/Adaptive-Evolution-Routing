# HotpotQA 300 题：三轮、三路并行 baseline

新配置：`experiments/configs/v0_1_codex_hotpotqa_gepa_300_r3.yaml`。
数据继续使用 `experiments/data/hotpotqa_300_manifest.json`，300 道当前题各配一条相似题、一条回归题，
共 900 道互不重复的题；选择标准见 [原数据协议](AER_HOTPOTQA_300_BASELINE.md)。
原一轮配置 `v0_1_codex_hotpotqa_gepa_300.yaml` 保留用于复现旧实验。

## 运行机制

### 公开反馈升级后的离线核对

2026-11-05 对既有 `hotpotqa-fixed-oscopilot-smoke-20261104-165543` 三题首轮记录进行了只读核对。
旧诊断回执未携带 metrics；新的公开反馈会携带原始 12 项指标，并按公开评分规则生成以下事实：

| task_id | 原 joint_f1 | 确定性解释 |
| --- | --- | --- |
| `5abe848555429976d4830b50` | 0.6 | `supporting_facts_exact`、`answer_excess_tokens` |
| `5a8dcc82554299441c6ba08e` | 0.0 | `supporting_facts_exact`；yes/no 评分特例由公开规则解释 |
| `5a794c215542994bb9456feb` | 0.8 | `answer_exact`、`supporting_facts_incomplete` |

这次核对未重新调用诊断模型，也未重新执行 Codex 或工具进化，因此不能作为诊断准确率或 baseline 得分改善的实验结果。
新实验使用 `aer.feedback.v2`；GEPA 与 OS-Copilot 比较时应统一该协议版本。字段、兼容性和证据校验边界见 [领域契约](AER_DOMAIN_CONTRACTS.md#公开评分与诊断协议-v2)。

- `fixed_router → gepa_prompt_evolver`，每个任务独立从初始 Prompt 开始。
- 外层最多三轮，稳定版本当前任务成功时提前结束；初始成功时不进化。
- 每轮 GEPA 内部仍最多两次反思、九次 trial 执行、每次评估三个样本；不是把内部搜索改成三轮。
- 接受策略：`0.5 Δcurrent + 0.25 Δsimilar + 0.25 Δregression >= 0.04`，
  且本轮回归 Joint F1 下降不超过 0.10。完整定义及复用方式见 [接受策略协议](AER_WEIGHTED_ACCEPTANCE_G004_R010.md)。
- 初始/最终成功率按 Joint EM 计算，优化与验收分数按 Joint F1 计算；LLM 仍负责诊断和反思。

## 命令

在仓库根目录用 PowerShell 执行；`run-id` 必须是未使用过的新目录名：

```powershell
.venv/Scripts/python.exe -m experiments run-benchmark experiments/configs/v0_1_codex_hotpotqa_gepa_300_r3.yaml --run-id hotpotqa-fixed-gepa-300-r3p3-v1 --workers 3 --report-holdouts
```

如果需要先做三题运行检查，增加 `--limit 3`，并使用另一个 `--run-id`。这会执行真实模型请求。
不传 `--workers` 时保持兼容的串行默认值 1；三路并行由上述命令显式开启。

## 并行与输出

调度器使用 spawn 进程池，最多同时处理三个不同任务；任务结束后动态补入下一个。
每个任务内部按顺序运行 AER 轮次、GEPA 搜索和验证。每个任务都有独立的 Agent 实例、版本库、
经验日志、缓存和运行目录，不从其他任务继承 Prompt。共享的 benchmark 源文件只读。
并行仅允许 `independent` 模式；`continual` 必须串行保持状态顺序。

父目录 `runs/<run-id>/` 保存总汇总、总预测文件及 `baseline_protocol.json`；
任务目录为 `runs/<run-id>/tasks/task-0001/` 等。父汇总按原始任务顺序展示，
每完成一个任务便更新磁盘上的汇总。任务条目记录 `run_root`、进程号和起止时间。
它的所有相对工件路径均以条目的 `run_root` 为基准。

普通单任务异常记录为 error，其他任务继续。进程意外终止使进程池失效时，已完成结果保留，
无法执行的任务显式记录为 error，费用标记为未知；不自动重试，也不将缺失评分移出已分配任务分母。
当前不提供中断续跑；重跑须使用新 run-id。

启动异常在子进程内转换成可序列化的错误数据，避免框架异常对象反序列化失败使整个进程池失效。
控制台 error 行包含具体类型和原因，子目录 `worker_error.json` 保存错误详情；
`baseline_summary.json` 的 `tasks[].error` 保留同样的信息。若进程池本身失效，额外保留可用的 cause。
已用过的 `hotpotqa-fixed-gepa-300-r3p3-v1` 不能直接重用；修复后重跑应换成 `...-v2` 等新名称。

`--report-holdouts` 记录所有任务初始/最终稳定版本的相似、回归表现，包括零轮和没有提交的任务。
相似题和回归题参与外层验收，因此报告是运行时验收表现，不是额外的独立最终测试。

`wall_elapsed_seconds` 是整批实际墙钟时间；`usage.elapsed_seconds` 是所计入操作耗时之和，
并行时不可当作整批运行时间。并行不会减少同等执行路径下的模型请求数或 Token，实际加速取决于模型端吞吐。
全局累计 Token/时间预算仍未强制执行，GEPA 单次调用/搜索超时等已有局部限制继续生效。

## Fixed-Tool / OS-Copilot

复用本文件开头的 Fixed-Prompt YAML，通过 CLI 覆盖切换目标；不编辑原 YAML，也不新增配置文件。
在独立 worktree 中执行以下命令，使用该 worktree 的 `.venv`、`.env`、数据缓存和 `runs/`。
先安装 `.[oscopilot]` 并初始化 `third_party/OS-Copilot`，不需要安装原生 quick_start 的完整依赖。
数据准备可使用 `experiments.prepare_hotpotqa --download-only`（需要 `.[hotpotqa-data]`），也可复制已有的
`.aer-cache/hotpotqa/`；加载时会按原 manifest 校验哈希。不要修改正在运行的工作区及其 Python 环境。

```powershell
$toolOverrides = @(
  '--set', 'experiment.id=hotpotqa-fixed-oscopilot-300-r3',
  '--set', 'routing.fixed_target=tool',
  '--set', 'targets.allowed=[tool]',
  '--set', 'targets.default_operators={tool: oscopilot_tool_evolver}',
  '--set', 'targets.operator_configs={tool: {max_generated_tools: 2, max_tokens: 200000, export_token_reserve: 20000, timeout_seconds: 900, model_timeout_seconds: 90, max_output_tokens: 4096, max_expanded_output_tokens: 16384}}',
  '--set', 'agent.config.tooling={catalog: {}, generated_tools: {enabled: true, runtime: python_jupyter}}',
  '--set', 'agent.config.initial_tools=[]',
  '--set', 'limits.max_output_tokens_per_call=16384'
)
.venv/Scripts/python.exe -m experiments validate-config experiments/configs/v0_1_codex_hotpotqa_gepa_300_r3.yaml @toolOverrides
.venv/Scripts/python.exe -m experiments run-benchmark experiments/configs/v0_1_codex_hotpotqa_gepa_300_r3.yaml @toolOverrides --run-id hotpotqa-fixed-oscopilot-smoke-v1 --limit 3 --workers 1 --report-holdouts
```

第二条命令调用真实模型；先检查小样本的构造、导出、MCP 调用和评分轨迹。正式运行去掉 `--limit 3`，
使用新的 `--run-id`，再按机器与接口负载决定并发。worktree 不隔离 CPU、内存和模型接口配额。
相同的 override 数组也可以用于 `inspect-benchmark`；两个 targets 映射必须整体替换，不能残留 prompt 项。

预算是每轮构造的初始调试设置，不是经真实 HotpotQA 优化后的参数：模型调用上限沿用 operator 默认 20，
单轮最多 200,000 tokens 和 900 秒，导出动态预留最多 20,000 tokens。外层输出上限 16,384 允许
OS-Copilot 从 4,096 扩容；导出示例仍限制 2,048 字符，正式调用可以传入更长上下文。
初始答题提示、模型、Joint F1/Joint EM、三轮提前停止、验收权重与阈值均保留。全局累计预算仍未强制执行。

OS-Copilot 使用公开任务、轨迹、结果和诊断构造候选工具库，Codex 在候选验证时通过 MCP 调用。
每题从空工具库独立开始；接受后提交，拒绝或空候选保留稳定版本。工具依赖调用者传入的数据，
给定上下文直接作为 JSON 参数，文档标题及句子编号应保持原样。已有 prompt 不禁止 MCP 调用；
不强制调用工具，不增加 gold 信息或额外答案生成模型。

批量报告新增：`initial_tool_library` / `final_tool_library`（工件引用及工具名）、`final_tool_usage`、
每轮 `tool_construction`（状态、错误、计划完成情况、Python 完成节点、导出/复验结果、阶段调用、成本），
以及候选验证的精简 `tool_usage_analysis`。总汇总中的 `aggregate.tool_construction` 按题和轮统计，
不是跨题全局去重数量；构造成本已包含在 episode usage 中，不再次累计。
原 GEPA 的 `internal_*` 字段继续保留，不能用其中的零 trial 数判断 OS-Copilot 没有工作。
工具声明的业务成功与官方答题评分分别记录，调用统计不证明因果收益。

执行边界：现有生成工具 worker 有独立 cwd、进程超时和凭据环境过滤，但不是操作系统文件/网络沙箱。
正式实验需要在执行环境中限制 worker 接触 gold 数据及外部知识来源；本适配不声称强化了这个边界。
离线测试验证构造模型上下文不包含私有评分规范或验证题输入；候选验证时，工具接收该次验证题的公开上下文。
这些检查不能替代操作系统访问控制验证。

离线组合测试（真实原生构造方法、Jupyter worker、MCP 和官方评分，模型与 Codex 响应为受控 fixture）：

```powershell
.venv/Scripts/python.exe -m unittest tests.integration.test_hotpotqa_oscopilot -v
```
