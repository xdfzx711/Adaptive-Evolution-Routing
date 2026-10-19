# AER v0.1 范围与实验语义

状态：开发步骤 1 的设计基线；日期：2026-10-13。

2026-10-16 扩展：新增 Qwen 驱动的 `llm_router`、`llm` 运行模式和 `execution_limits` 步数进化目标。实现及调用日志见 [Qwen 路由与执行步数进化](AER_LLM_ROUTING.md)。验证、接受和提交语义沿用原有规则。

本文落实 `AER_MVP.md` 的首版范围。两份文档有冲突时，v0.1 的配置字段、轮数、反馈权限和接受规则以本文为准。修改实验策略时更新策略版本和运行 manifest；不把策略阈值写入 Orchestrator。

步骤 1–4 已交付配置、领域接口与插件装配；步骤 5 已实现 [本地工件、版本和经验存储](AER_LOCAL_STORAGE.md)，步骤 7 已实现 [本地 Benchmark 与评分](AER_LOCAL_BENCHMARK.md)，步骤 8 已实现 [Agent 执行与诊断](AER_AGENT_EXECUTION.md)，步骤 9 已实现 [上下文、路由与候选生成](AER_PROMPT_EVOLUTION.md)，步骤 10 已实现 [配对验证与接受选择](AER_PAIRED_VALIDATION.md)，步骤 11 已实现 [主控制器、提交与复测](AER_ORCHESTRATOR.md)。插件装配见 [注册与依赖装配](AER_PLUGIN_ASSEMBLY.md)，接口见 [领域模型与同步接口](AER_DOMAIN_CONTRACTS.md)，使用方法见 [README](../README.md)。批量 Runner 和实验汇总尚待实现。

2026-10-14 MVP 范围调整：完整预算账本、tokens/工具次数硬上限、episode 总时间限制及连续无提交终止延后实现。本文相关字段和预算契约保留为后续设计，当前只校验配置，不宣称限制已生效。步骤 11 已实现任务成功、进化关闭、配置轮数、Manual 计划耗尽、无可行目标和不可恢复错误等基本停止条件。

## 1. 首版组件与边界

| 组件 | v0.1 实现约定 | 后续扩展 |
| --- | --- | --- |
| Agent | `react_agent`；`single_call_agent` 作为第二个最小 Agent，单次模型调用、不使用工具 | 第三方 Agent、Code Agent、多 Agent |
| 模型与工具 | 注入 ModelProvider、ToolProvider；`scripted_model` 用于工程验收，`qwen_model` 接入真实 API，工具由 `deterministic_tools` 提供 | 更多模型 Provider 与正式实验 |
| Router | `fixed_router`、`manual_router`、`llm_router`；只决定 target | 学习型 Router |
| Evolver | `simple_prompt_evolver` 产生 Prompt；`step_limit_evolver` 产生步数工件；每轮候选数由配置限制 | Memory、Tool、Code 等 Evolver |
| Benchmark | `toy_tasks`，公开任务输入与私有评分规格分离 | 正式 Benchmark Adapter |
| Evaluator | `deterministic_evaluator`；根据评分规格输出标准化分数与成功标记 | 更多任务评分方法 |
| Diagnoser | `lightweight_diagnoser`；基于可见轨迹和反馈生成标签与证据，不选择 target | 模型诊断 |
| Harness | `paired_validation`；配对执行、分组统计、调用接受策略 | 多种验证策略 |
| 接受策略 | `improvement_with_regression_guard`，策略版本 `v0.1` | 可替换接受策略、成本奖励 |
| 存储 | `local_files`、`local_manifest`、`jsonl` | 数据库、远程对象存储 |
| Runner | `aer`；支持四种模式，同步执行 | External Baseline Runner |

当前可执行目标是小写 `prompt` 和 `execution_limits`，后者仅支持 ReAct。目标由 Agent 与 Evolver 的插件能力及当前工件共同约束，不在核心配置中固定目标名称。Agent 可以具有不同内部执行方式，但都须支持从 Snapshot 重建独立会话、标准结果与成本输出。

`single_call_agent` 用于验证替换能力，不要求与 ReAct Agent 有相同表现。`scripted_model` 的固定响应和模拟成本只用于工程路径验收，不用于宣称进化或路由的研究收益。两个目标与 Qwen 路由已具备状态选择的执行路径，决策质量需要后续实验评估。

## 2. 运行模式

`evolution.mode` 是运行模式的唯一来源，不再同时设置 `evolution.enabled`。

| mode | routing.plugin | 决策配置 | 行为 |
| --- | --- | --- | --- |
| `disabled` | `null` | `fixed_target=null`，`manual_decisions=[]` | 执行与评分一次，不实例化 Router/Evolver/Harness，不改变版本 |
| `fixed` | `fixed_router` | `fixed_target=prompt`，`manual_decisions=[]` | 每个进化轮选择同一个可行 target |
| `manual` | `manual_router` | `fixed_target=null`，显式提供轮次决策列表 | 按 round_id 使用预先指定目标，无运行时交互 |
| `llm` | `llm_router` | `fixed_target=null`，`manual_decisions=[]` | 模型根据允许反馈及可行目标选择本轮 target |

配置文件：

- `experiments/configs/v0_1_no_evolution.yaml`
- `experiments/configs/v0_1_fixed_prompt.yaml`
- `experiments/configs/v0_1_manual_prompt.yaml`

Manual 决策项由 `round_id`、`target`、`rationale` 构成。`round_id` 从 1 开始，唯一、连续、按升序排列，列表可短于最大轮数，不能长于最大轮数。最大轮数为正数时 Manual 列表须非空，为 0 时列表须为空。决策表在每个任务开始时从第 1 轮重新使用；下一轮没有项时，以 `manual_plan_exhausted` 停止，不自动重复最后一项或使用 Fixed Router。

Manual 可以指定 Agent 与 Evolver 共同支持的目标。与 Fixed 相同的目标序列可能产生相同结果，不能将其视为两种独立算法收益。它验证的是人工决策输入与统一路由契约。上表 routing.plugin 是真实组件的默认名称；允许选择声明支持对应 mode 的其他 Router，不将 mode 绑定到单个插件 ID。

## 3. 轮数、执行与停止

### 3.1 轮数

`max_evolution_rounds` 是每个任务的进化尝试上限，非 Agent 模型步数、候选数或任务执行次数。数值从配置读取，无代码内固定值或隐式默认值。

初始任务执行不计轮。初始执行失败后，完成停止条件与可行目标检查，在调用 Router 前创建 EvolutionAttempt，并分配从 1 开始的 round_id。该尝试随即占用一轮：路由异常、Evolver 异常、空候选、拒绝和验证中断都计轮；多个候选仍只计一轮。进入 Router 之前发现预算不足、无可行目标或 Manual 计划耗尽，不占新的一轮，但要保存任务停止记录。

`disabled` 模式要求 `max_evolution_rounds=0`。`fixed/manual/llm` 允许非负整数，0 表示只执行初始任务；记录 `max_evolution_rounds_reached`，不与显式 disabled 模式混淆。

### 3.2 执行与复测

每个任务先使用初始稳定版本执行并外部评分；成功则停止，失败才可能进化。候选通过验证并成功提交后，使用新的独立会话复测当前开发任务，即使这已是最后允许的进化轮，也不得省略复测。

候选全部被拒绝时，稳定版本没有变化，可以复用该版本最近的当前任务执行与评分，重建包含本轮反馈的上下文；无需无条件重复执行相同版本。候选在 Local Validation 中执行当前任务属于候选评估，不计新的进化轮，也不代替提交后的新会话复测。

若提交后预算不足、执行中断或无法评分，保留新稳定版本，但 `final_evaluation_status=not_evaluated`，`final_execution/final_evaluation=null`；另存旧版 `last_evaluated_execution/last_evaluated_evaluation` 及其版本。不得将旧版任务分数绑定到新版本。

### 3.3 停止与错误

| stop_reason | 语义 |
| --- | --- |
| `task_succeeded` | 当前稳定版本对应的当前任务外部评分成功 |
| `evolution_disabled` | 初始任务已评分且失败，配置关闭进化 |
| `max_evolution_rounds_reached` | 已用完配置的轮数，当前稳定版本任务未成功 |
| `budget_exhausted` | 剩余资源不足、时间上限到达或无法完成后续必要调用 |
| `no_feasible_targets` | 没有 Agent、Operator 与约束共同允许的目标 |
| `manual_plan_exhausted` | 下一轮没有人工指定决策 |
| `no_commit_limit_reached` | 达到配置的连续无提交轮数 |
| `execution_error` | 当前任务运行无法形成可评分结果，或复测发生不可恢复执行错误 |
| `evaluation_error` | 当前任务评分失败；不得当作成功率 0 的普通任务失败 |
| `diagnosis_error` | 失败分析或诊断契约异常，在新进化尝试开始前停止 |
| `storage_error` | 工件、版本或经验持久化异常；不能伪造已经保存的证据 |
| `routing_error` | Router 异常或返回不可行 target |
| `evolution_error` | Evolver 异常或输出违反工件/候选契约 |
| `validation_error` | 验证基础设施或评分异常，无法形成完整候选对照 |
| `commit_error` | 版本提交失败，稳定指针保持提交前版本 |

预算中断优先记录 `budget_exhausted`，具体发生阶段另存。成功评分优先于其他停止条件。未成功时，按 disabled、轮数上限、无提交上限、预算、Manual 计划、可行目标顺序检查；仅在需要进入新一轮时调用 Diagnoser/Router。PluginError 首版记录后停止，框架不自动重试；Provider 内部如有重试，须记录每次调用及其成本。

空候选或普通拒绝不属于基础设施异常，可以继续下一轮。`max_consecutive_no_commit_rounds=null` 表示不启用额外停止条件；正整数表示连续这么多轮没有成功提交就停止，提交后清零。重复选择 `prompt` 本身不是停止条件。

当前 MVP 暂缓预算和连续无提交停止，运行时按 success、disabled、轮数、Manual、可行目标顺序检查。诊断只在检查通过后执行。任一候选验证基础设施异常或用量不完整，保留本批次证据并停止，不提交该批次候选；完整对照下的普通拒绝继续下一轮。提交回执恢复属于完成已发布提交的持久化，不重新生成或验证。

## 4. 预算、资源单位与配对执行

预算作用域固定为当前任务完整 episode：初始执行、诊断、路由、候选生成、Local/Similar/Regression 验证、提交后复测都计入。每个任务重新分配配置预算，run 汇总累加所有任务成本。累计版本模式也不累计跨任务预算。

| 字段 | 单位和计算规则 |
| --- | --- |
| `input_tokens` | 所有模型请求实际输入 token 数，含 Provider 报告的缓存输入；不重复增加缓存分项 |
| `output_tokens` | 所有模型响应实际输出 token 数；包含 Provider 报告的计费推理 token 时不得重复计数 |
| `total_tokens` | `input_tokens + output_tokens`，与 `limits.max_tokens` 比较 |
| `model_calls` | 每次实际发出的模型请求记 1，含失败、内部重试 |
| `tool_calls` | 每次实际派发的工具调用记 1，含失败；声明工具不计调用 |
| `elapsed_seconds` | 对应记录作用域的单调时钟耗时；episode 总量从开始到结束测量，含框架开销，不累加嵌套阶段耗时 |
| `usage_source` | `provider_reported`、`simulated` 或 `unknown`；混合来源以分项明细保存 |

调用前检查并预留预算；模型输出上限不得大于可用输出额度，实际用量返回后结算，释放未使用预留额度。只有余额不足或有未结算额度而无法预留下一次调用时才阻止调用。Provider 超时/失败没有报告实际 token 时，保留 usage 不完整标记与预留记录，首版停止，不伪造已知的 0 消耗或继续扩大未结算用量。

`model_calls` 是记录指标，v0.1 不设置额外调用次数上限。时间上限是 episode 整体上限，单次模型/工具超时分别来自配置，并受剩余 episode 时间限制。框架同步执行不能将未完成线程视为已经取消；Provider/ToolProvider 必须声明可落实的超时行为。

配对任务顺序固定。配对 seed 由 experiment.seed、当前开发任务 ID、验证任务 ID 和重复编号稳定派生，不含候选 ID、版本 ID 或“旧版/候选”标记，以便两侧条件一致。v0.1 每个验证任务执行一次，不做统计显著性声明。真实 Provider 不支持 seed 时如实写入 manifest，不声称模型输出完全可复现。

## 5. 反馈权限与任务划分

### 5.1 可见数据

| 数据 | Agent | Diagnoser / Router / Evolver | Evaluator / Harness | 研究者结果文件 |
| --- | --- | --- | --- | --- |
| 当前任务公开 input、公开 metadata | 可见 | 可见 | 可见 | 可见 |
| 当前任务执行轨迹与输出 | 本次执行可见 | 可见，经允许字段筛选 | 可见 | 可见 |
| 当前开发任务 score、success、公开 reason_code/feedback | 不直接注入执行输入 | 可见 | 可见 | 可见 |
| 正确答案、私有评分规格、隐藏测试内容 | 不可见 | 不可见 | 可见 | 私有评估工件保存，不进入公开反馈 |
| 验证任务输入 | 执行该任务时可见 | 不进入进化上下文 | 可见 | 可见 |
| 验证逐任务结果、输出和轨迹 | 仅本次执行 | 不可见 | 可见 | 可见 |
| 历史验证聚合分数差、回归变化、接受与提交状态 | 不直接注入执行输入 | 可见 | 可见 | 可见 |
| 最终测试分数与逐任务结果 | 仅执行测试任务 | 不可见 | 最终评测可见 | 可见 |

`EvaluationResult` 是评估侧完整记录，不能直接作为公开上下文输入。ContextBuilder 产生 `FeedbackView` 与 `TaskView`：采用明确允许字段构建，拒绝私有评估引用、标签和可绕过权限的工件引用。Diagnoser 也使用该反馈视图。历史检索只返回当前时间已完成且允许公开的摘要，不返回完整私有评估记录或其他任务答案。

本地存储隔离是受信任插件的数据访问约定，不等同于隔离不受信任 Python 插件的安全沙箱。

### 5.2 划分

开发任务用于失败分析和进化触发。独立验证 split 内按公开元数据 `validation_group` 分为 `similar` 与 `regression` 两组，两组非空、互斥；该任务集合固定，不根据候选效果调整。

开发、验证、最终测试的任务身份集合必须互斥。身份由 `benchmark_id + task_id` 确定；Benchmark Adapter 还应检查其提供的来源 ID/重复样本信息。隐藏标签不写入公开 Task metadata。步骤 7 的 toy_tasks 固定为 dev 4 条、validation 两组各 4 条、test 4 条，真实索引由 Adapter 生成并供 setup 检查。

最终测试在开发进化结束后，以冻结 Snapshot 单独执行且禁用进化，通过 `agent.initial_snapshot_ref` 指定本地 Snapshot manifest。即使测试任务失败，也不能启动诊断驱动的进化或将测试结果放回后续进化历史。示例运行 dev；不能直接把启用进化配置的 execution_split 改成 test。独立任务模式的每个任务有自己的最终版本；应明确指定某个版本或分开评测，不能把各任务针对自身开发反馈的最优输出合并成一个所谓全局版本。

## 6. 跨任务状态与重置

`state.task_mode` 支持 `independent`、`continual`。示例使用 `independent`。

| 状态 | independent | continual |
| --- | --- | --- |
| 下一个开发任务起始版本 | 实验初始 Snapshot | 上个任务最终稳定 Snapshot |
| 可见进化经验 | 当前任务已完成尝试摘要 | 当前 run 已完成开发任务和当前任务的允许摘要 |
| 会话消息、工具运行状态、临时工作目录 | 每次执行清空/重建 | 每次执行清空/重建 |
| 预算和 round_id | 每个任务重置 | 每个任务重置 |
| 持久工件、历史结果文件 | 保留，但不越过可见性边界 | 保留，但不越过可见性边界 |

continual 的任务顺序来自固定 Benchmark 清单并写入 manifest；不得在运行中重排。可累计状态为已提交 Snapshot 的工件，包括 Prompt 与步数，不存在隐式长期 Memory。

`reset(task)` 清理会话与临时环境，并应用 task_mode；`reset(episode)` 恢复当前任务起始版本、预算和轮数，新的 episode_id 防止记录混淆；`reset(benchmark)` 恢复实验初始版本和学习历史可见范围。物理存储不因 reset 删除。独立验证的每个旧版/候选任务都重建会话，不更新实验稳定指针或共享经验。

## 7. 验证、接受、选择与提交

### 7.1 指标

Evaluator 输出有限分数 `score ∈ [0,1]`；二元任务使用 0/1。success 由 Benchmark 定义，不能从 Agent 自报获得。不同量纲原始指标由 Adapter 转为标准化 score，其规则写入 manifest。

令 A 为同类验证集，B 为回归验证集，V 为两者不重复的并集。每个独立验证任务权重相同：

\[
S(s)=\frac{1}{|V|}\sum_{q\in V}\mathrm{score}(q,s),\quad
S_A(s)=\frac{1}{|A|}\sum_{q\in A}\mathrm{score}(q,s),\quad
S_B(s)=\frac{1}{|B|}\sum_{q\in B}\mathrm{score}(q,s).
\]

\[
\Delta S=S(c)-S(b),\quad \Delta S_A=S_A(c)-S_A(b),\quad
\Delta S_B=S_B(c)-S_B(b).
\]

统一指标是 V 的任务平均分，不是两组平均分再平均。额外记录成功率、回归中原成功现失败的任务数和各阶段成本。回归门槛作用于组平均分，不承诺每个任务均无下降。

Local Validation 记录当前失败任务的旧版与候选表现，旧版可复用当前任务结果；候选使用独立会话执行。它不进入 V、不计 reward，也不是首版接受的硬条件。因此候选可以改善独立任务但未修复当前任务，提交后仍会继续当前任务闭环。

### 7.2 接受和奖励

通过条件全部满足：工件有效、parent_version_id 匹配当前稳定版本、Local 与两组独立验证完整结束、`ΔS > 0`、`ΔS >= min_improvement`、`ΔS_B >= -max_regression_drop`。空验证集、缺失任务结果或无穷/NaN 分数不能形成有效对照，不用 0 分填补基础设施错误。

有效独立对照的 `reward=ΔS`，即使候选被回归门槛拒绝或未选中，也保存该值及具体原因。没有完整独立对照时 reward 为 null，不把未知收益作为零收益训练样本。初版奖励不含 Local 分数和成本惩罚。

多个候选通过规则时按 candidate 的 S 降序、candidate_validation_tokens 升序、candidate_id 字典序升序选择一个。candidate_validation_tokens 仅计该候选 Local 与独立验证的模型输入加输出，不计共用的旧版验证或候选生成成本；所有实际发生的成本仍计入 episode 预算。该用量不完整的候选验证不能认定完整。candidate_id 必须唯一，选择结果按 ID 关联，不依赖列表 zip 顺序。

### 7.3 提交状态

`accepted` 表示通过接受策略；`selected` 表示被选中准备提交；`committed` 表示稳定指针实际已切换。三者分开保存。一个候选可以 accepted=true、selected=false；提交失败可以 accepted=true、selected=true、committed=false。

Version Manager 在验证前创建不可变候选 Snapshot，验证始终使用它。提交只将稳定指针切换到这个相同的 version_id，不在提交阶段重新生成 Prompt。提交前检查父版本仍匹配，先持久化候选与验证证据，再执行指针切换并记录提交事件；失败不能覆盖旧指针。恢复时以持久稳定指针和提交事件核对真实提交状态，不能仅由 accepted 推断。

拒绝意味着丢弃候选运行实例并保留稳定指针；工件和记录仍可追踪。不原地修改稳定 Snapshot，嵌套 dict/list 也需防御性复制或只读结构。文档中的 frozen dataclass 不是深层不可变的充分条件。

## 8. 完整配置字段契约

类型标记 `?` 表示允许 null；除表中明确说明之外，以下字段都须显式提供。未知字段报配置错误，不静默忽略。数量字段必须为整数且不接受 bool；浮点字段为有限数。null 的语义不等于缺少字段。

### 8.1 实验、组件与状态

| 字段 | 类型 / 约束 | 含义 |
| --- | --- | --- |
| `schema_version` | string，`aer.v0.1` | 配置契约版本 |
| `experiment.id` | 非空 string | 实验名；执行时再分配唯一 run_id |
| `experiment.seed` | 非负 int | 基础种子，进入派生 seed 与 manifest |
| `experiment.runner` | string，`aer` | Runner 标识 |
| `benchmark.plugin` | string，首版 `toy_tasks` | Benchmark 标识 |
| `benchmark.execution_split` | `dev` / `test` | 当前任务来源；test 仅允许 disabled |
| `benchmark.task_ids` | list[string]? | null 为 split 全部任务，列表须非空、无重复且属于该 split；按 Benchmark 顺序过滤 |
| `state.task_mode` | `independent` / `continual` | 当前 run 的跨开发任务状态模式 |
| `agent.plugin` | 已声明的 Agent ID | 首版真实组件为 react_agent/single_call_agent；Mock 与后续工厂由注册元数据加入 |
| `agent.initial_prompt` | 非空 string? | 初始 Prompt 文本，setup 时转为不可变工件；与 initial_snapshot_ref 二选一，示例采用同一文本 |
| `agent.initial_snapshot_ref` | 非空 string 路径? | 既有 Snapshot manifest；相对 YAML 目录解析，工件引用按原存储位置加载；首版仅 disabled 可用，test 必须使用它 |
| `agent.max_steps` | 正 int | 单次 Agent 执行的最大模型决策步；single_call_agent 要求 1 |
| `providers.model.plugin` | 非空 string | ModelProvider；示例 `scripted_model` |
| `providers.model.model_id` | 非空 string | 模型/脚本标识；不得被解释为凭据 |
| `providers.model.config` | object | Provider 专用配置，经对应插件 schema 校验 |
| `providers.tools.plugin` | 非空 string | ToolProvider；示例 `deterministic_tools` |
| `providers.tools.enabled` | list[string] | 暴露的工具名称，无重复；single_call_agent 不使用工具 |
| `assessment.evaluator` | string，首版 `deterministic_evaluator` | 外部评分插件 |
| `assessment.diagnoser` | string，首版 `lightweight_diagnoser` | 诊断插件，disabled 不实例化 |

示例 Provider 的专用配置只有 `scenario_id: string`，声明为 `mvp_v0_1_contract` 场景，后续确定性响应实现遵循此 ID。该标识不意味着已经存在场景或真实模型调用。

### 8.2 进化、验证与可见性

| 字段 | 类型 / 约束 | 含义 |
| --- | --- | --- |
| `evolution.mode` | `disabled` / `fixed` / `manual` / `llm` | 唯一运行模式 |
| `evolution.max_candidates_per_round` | 正 int | Evolver 单轮返回上限，示例为 1；多候选接口不改轮数 |
| `routing.plugin` | string? | 已声明 Router，能力包含当前 mode；disabled 为 null |
| `routing.fixed_target` | string? | 仅 fixed 非空，必须属于 allowed |
| `routing.manual_decisions` | list[ManualDecision] | 仅 manual 且最大轮数大于 0 时非空；见第 2 节 |
| `targets.allowed` | list[string] | disabled 为空；启用进化时非空，目标由插件能力检查 |
| `targets.default_operators` | map[string,string] | allowed 中每个目标的具体 Evolver；disabled 为空 |
| `targets.operator_configs` | map[string,object]，默认空 | 各目标 Operator 专用参数，由对应插件 schema 校验 |
| `validation.plugin` | string，`paired_validation` | 验证插件，disabled 不实例化 |
| `validation.split` | string，`validation` | 独立验证划分 |
| `validation.group_field` | 非空 string，示例 `validation_group` | 公开分组元数据字段，实际归属在任务索引/Benchmark setup 检查 |
| `validation.similar_group` | 非空 string，示例 `similar` | 同类组值 |
| `validation.regression_group` | 非空 string，示例 `regression` | 回归组值，不能与 similar 相同 |
| `validation.acceptance_policy` | string，`improvement_with_regression_guard` | 可替换策略标识 |
| `validation.policy_version` | string，`v0.1` | 策略语义版本，修改策略时更新 |
| `validation.min_improvement` | float，范围 [0,1] | 正提升的最小阈值；即使为 0 也拒绝 ΔS=0 |
| `validation.max_regression_drop` | float，范围 [0,1] | 回归组平均分允许下降幅度 |
| `feedback.policy` | string，`dev_feedback_and_validation_summary` | 第 5 节允许字段规则；只支持该策略 |

disabled 配置仍保留完整验证字段以便对照追踪，加载时校验字段类型和策略版本，但不加载验证任务或插件。启用进化时必须检查分组非空、划分互斥和目标/Operator/Agent 兼容性。加载既有 Snapshot 时检查 agent.plugin 和运行配置与冻结版本兼容；不以 YAML 的另一个 Prompt 覆盖冻结版本，不迁移或重写版本内容。

### 8.3 上限与存储

| 字段 | 类型 / 约束 | 含义 |
| --- | --- | --- |
| `limits.max_evolution_rounds` | 非负 int | 每个开发任务的进化尝试上限 |
| `limits.max_tokens` | 正 int | episode 总输入加输出 token 上限 |
| `limits.max_tool_calls` | 非负 int | episode 实际工具调用上限，0 禁止调用工具 |
| `limits.max_elapsed_seconds` | 正 float | episode 总时长上限 |
| `limits.model_call_timeout_seconds` | 正 float | 单次模型请求超时 |
| `limits.tool_call_timeout_seconds` | 正 float | 单次工具调用超时 |
| `limits.max_output_tokens_per_call` | 正 int | 模型响应额度上限，不大于 max_tokens，运行时还受余额限制 |
| `limits.max_consecutive_no_commit_rounds` | 正 int? | 连续无提交停止阈值，null 关闭 |
| `storage.root` | 非空 string 路径 | 相对路径相对 YAML 所在目录解析，不依赖 shell 工作目录；每次运行创建独立 run_id 子目录 |
| `storage.experience` | string，`jsonl` | 经验事件存储 |
| `storage.artifacts` | string，`local_files` | 工件存储 |
| `storage.versions` | string，`local_manifest` | Snapshot 与稳定指针存储 |

模型凭据不写入 YAML 或 manifest；真实 Provider 的连接参数留待实现该插件时定义。解析后的配置、schema/API/plugin/policy 版本、模型能力、seed、任务顺序和评分规则进入 manifest。

## 9. 数据类型映射

以下为已实现类型的字段映射，基础 Task/TraceEvent/ArtifactRef 字段沿用开发指导。配置类型在 `aer_core/config/models.py`，领域类型在 `aer_core/schemas/`。表中的 list/map 表示序列化格式，领域对象内使用 tuple/只读 Mapping；RunContext 和 EvolutionRequest 持久化时转换为对应 Record。

### 9.1 配置和执行

| 类型 | 字段 |
| --- | --- |
| `ExperimentConfig` | schema_version: str；experiment: ExperimentSettings；benchmark: BenchmarkSettings；state: StateSettings；agent: AgentSettings；providers: ProviderSettings；assessment: AssessmentSettings；evolution: EvolutionSettings；routing: RoutingSettings；targets: TargetSettings；validation: ValidationSettings；feedback: FeedbackSettings；limits: BudgetLimits；storage: StorageSettings；各嵌套字段与第 8 节一一对应 |
| `ManualDecision` | round_id: int；target: str；rationale: str |
| `BudgetLimits` | 第 8.3 节所有 limits 字段；运行控制阈值与资源额度分别使用 |
| `BudgetRemaining` | tokens/tool_calls: int；elapsed_seconds: float；值已扣除未结算预留，tokens 为总 token 可用额度 |
| `BudgetHandle` | 运行时预算接口，提供 remaining/reserve/settle 操作，不作为 Snapshot 或 JSON 内容序列化 |
| `UsageReport` | input_tokens/output_tokens: int?；model_calls/tool_calls: int；elapsed_seconds: float；usage_source: str；不含预留列表 |
| `ResourceUsage` | input_tokens/output_tokens/total_tokens: int?；model_calls/tool_calls: int；elapsed_seconds: float；usage_complete: bool；usage_sources: list[str]；pending_reservations: list[Reservation] |
| `Reservation` | operation_id: str；stage: str；reserved_tokens/reserved_tool_calls: int；status: pending/settled；reported_usage: UsageReport? |
| `RunContext` | run_id/episode_id/operation_id: str；stage: str；seed: int；budget: BudgetHandle?（预算暂缓时为 null）；workspace_ref: str；model_timeout_seconds/tool_timeout_seconds/max_output_tokens: 调用参数 |
| `ExecutionResult` | task_id/agent_version_id/operation_id: str；output: object；trajectory: list[TraceEvent]；state_summary: object；usage: ResourceUsage；artifact_refs: list[ArtifactRef]；status: completed/step_limit/budget_interrupted/error；error: ErrorInfo? |
| `EvaluationResult` | evaluation_id/task_id/agent_version_id/execution_operation_id/operation_id: str；score: float；success: bool；metrics: map[str,float]；feedback: str；reason_code: str?（步骤 7 新增，旧记录默认 null）；evidence_refs: list[ArtifactRef] |
| `RunContextRecord` | RunContext 的可持久化字段；budget 替换为 remaining_budget: BudgetRemaining?，预算暂缓时为 null，不序列化实时句柄 |
| `TaskExecutionRecord` | run_id/episode_id、TaskIdentity、ExecutionResult、可选 EvaluationResult/FeedbackView/Diagnosis/assessment_error；仅记录执行与评估，不代替完整 RunResult |
| `TaskView` | task_id/benchmark_id: str；input: object；public_metadata: object；不含 eval_spec_ref 或评分工件引用 |
| `FeedbackView` | score: float；success: bool；reason_code: str；public_feedback: str；evidence_step_ids: list[int] |
| `ExecutionView` | task_id/agent_version_id: str；output: 公开输出；trajectory: list[允许字段的 TraceEvent]；state_summary: 公开状态；usage: ResourceUsage；不含私有评估工件引用 |
| `Diagnosis` | reason_code: str；explanation: str；evidence_step_ids: list[int]；confidence: float?；只根据公开视图生成 |

正常结束、步骤上限或可评分的工具失败由 Evaluator 判定，不自动当作基础设施错误。usage 来源明细按 operation_id 聚合，预算统一账本中的同一次调用只计算一次；禁止同时累加外层总量和内层调用量。

### 9.2 进化与验证

| 类型 | 字段 |
| --- | --- |
| `AgentCapabilities` | supported_targets: list[str]；supported_artifact_kinds: list[str]；can_rebuild_session: bool；snapshot_scope: str；支持从 Snapshot 重建独立会话是首版进化条件 |
| `EvolutionCapabilities` | supported_targets/compatible_agents/required_artifact_kinds: list[str]；supports_multiple_candidates: bool；reports_usage: bool |
| `EvolutionContext` | round_id: int（旧记录默认 1）；task: TaskView；stable_snapshot: AgentSnapshot 公开投影；execution: ExecutionView；feedback: FeedbackView；diagnosis: Diagnosis?；history: list[ExperienceSummary]；feasible_targets: list[str]；remaining_budget: BudgetRemaining?；as_of: UTC 时间 |
| `EvolutionDecision` | target: str；rationale: str；不允许 Router 指定 operator_id，首版统一由 Resolver 决定 |
| `EvolutionRequest` | attempt_id: str；round_id: int；context: EvolutionContext；decision: EvolutionDecision；operator_id: str；max_candidates: int；run_context: RunContext |
| `EvolutionRequestRecord` | 与 EvolutionRequest 对应，run_context 使用 RunContextRecord |
| `EvolutionResponse` | candidates: list[EvolutionCandidate]；usage: ResourceUsage；status: completed/empty/error/budget_interrupted；error: ErrorInfo? |
| `EvolutionCandidate` | candidate_id/parent_version_id/target: str；artifact_updates: 非空 map[str,ArtifactRef]，key 与 kind 一致；candidate_version_id: str?（Version Manager 创建候选 Snapshot 后填写）；metadata: object；status: generated；工件更新通用，实际能力由插件声明 |
| `ValidationPlan` | current_task: Task；baseline_execution: ExecutionResult?；baseline_evaluation: EvaluationResult?（旧版结果同时提供或同时为 null，null 表示重新执行）；similar_tasks/regression_tasks: list[评估侧 Task]；pair_seeds: list[PairSeed]；policy_id/policy_version: str |
| `PairSeed` | task: TaskIdentity；seed: int；每个独立验证任务恰好一个配对种子 |
| `ValidationMetrics` | score/similar_score/regression_score/success_rate: float；regression_failure_count: int；task_count/similar_count/regression_count: int |
| `ValidationReport` | validation_id/candidate_id/baseline_version_id/candidate_version_id: str；status: complete/incomplete/error；baseline_metrics/candidate_metrics: ValidationMetrics?；local_result_ref/private_results_ref: ArtifactRef?；candidate_validation_tokens: int?；usage: ResourceUsage；error: ErrorInfo? |
| `AcceptanceResult` | candidate_id: str；accepted: bool；reward: float?；reason: str；delta_score/delta_similar_score/delta_regression_score: float?；policy_id/policy_version: str |
| `LocalValidationMetrics` | baseline_score/candidate_score: float；baseline_success/candidate_success: bool；delta_score: float；repaired: bool；ValidationReport.local_metrics 为可选字段以兼容旧报告 |
| `ScoredExecution` / `PairedTaskResult` | 任务身份、Snapshot、seed、执行/评分/成本/异常；旧/新两侧身份与 seed 相同，版本不同，标记旧版缓存命中 |
| `ValidationBatchResult` | run/episode/attempt/stable 身份、reports/outcomes、selected_candidate_id、实际批次 usage、经验引用；本阶段 committed=false |
| `ExperienceSummary` | attempt_id/run_id/episode_id/task_id/stable_version_id: str；round_id: int；decision/diagnosis: 公开摘要；candidate_summaries: list[CandidateSummary]；completed_at: UTC 时间；不含验证逐任务工件引用 |

AgentSnapshot 中 config 和 artifacts 均代表可控、可版本化的状态；不包含模型客户端、凭据、预算对象或存活会话。Snapshot 的 version_id 与 parent_version_id 跟踪版本谱系，工件摘要用于内容身份和版本保持检查。

### 9.3 经验与最终输出

| 类型 | 字段 |
| --- | --- |
| `EvolutionAttempt` | attempt_id/run_id/episode_id/task_id: str；round_id: int；stable_version_id: str；pre_state_ref/trajectory_ref: ArtifactRef；feasible_targets: list[str]；decision: EvolutionDecision?；operator_id: str?；candidate_ids: list[str]；status: started/completed/empty_candidates/error/budget_interrupted；error: ErrorInfo?；diagnosis: Diagnosis?；budget_before/budget_after: BudgetRemaining?；usage: ResourceUsage；started_at: UTC 时间；finished_at: UTC 时间? |
| `CandidateEvaluation` | attempt_id/candidate_id: str；candidate_version_id: str?（生成失败前可能未物化）；validation_ref: RecordRef?；acceptance: AcceptanceResult?；status: CandidateStatus；selected/committed: bool；commit_error: ErrorInfo?；final_recorded_at: UTC 时间 |
| `RunResult` | run_id/episode_id/task_id: str；initial_version_id/final_stable_version_id: str；status: RunStatus 终态；final_evaluation_status: evaluated/not_evaluated；final_execution: ExecutionResult?；final_evaluation: EvaluationResult?；last_evaluated_execution/last_evaluated_evaluation: 对应结果或 null；evolution_rounds_used: int；stop_reason: StopReason；stop_stage: RunStage；experience_refs: list[RecordRef]；usage: ResourceUsage；error: ErrorInfo? |
| `ExperimentResult` | task_id/method_id/run_id/episode_id: str；seed: int；mode/task_mode: str；initial_version_id/final_stable_version_id: str；run_result_ref: RecordRef；eval_metrics: object?；resource_usage: ResourceUsage；trace_refs/artifact_refs: list[ArtifactRef]；provenance: ManifestRef |
| `ErrorInfo` | code/stage/message: str；operation_id/plugin_id: str?；evidence_ref: ArtifactRef?；usage: ResourceUsage?；不混入模型凭据 |

JSONL 采用事件追加：attempt_started、attempt_finished、candidate_evaluated、version_committed、task_finished。每条事件有唯一 event_id、record_type、schema_version、run_id、episode_id、UTC timestamp 和类型化 payload。查询按 ID 重建尝试状态，不以覆盖旧记录模拟更新。

未验证、验证中断或 Evolver 没有产生候选的尝试仍需有记录；未开始进化的初始执行与停止原因保存在 task_finished。每个分数与轨迹都有明确 task/version/operation 身份。final_execution 非 null 时必须匹配 final_stable_version_id；没有对应评分时汇总保持 null，不填 0。

## 10. 本步骤验收与后续实现清单

| 验收项 | 已明确的依据 |
| --- | --- |
| 首版组件固定 | 第 1 节，明确第二个 Agent 与 Prompt-only 边界 |
| 三种模式可区分 | 第 2 节及三份完整 YAML；无双重 enabled/mode 状态 |
| 轮数不写死 | 第 3 节、limits 配置；初始执行、失败尝试、复测明确区分 |
| 预算单位与作用域明确 | 第 4 节、第 8.3 节；每个任务完整 episode 计量 |
| 反馈权限明确 | 第 5 节、TaskView/FeedbackView/ExperienceSummary |
| 跨任务状态明确 | 第 6 节；independent 与 continual 不隐式混用 |
| 接受与提交明确 | 第 7 节；指标、阈值、平分规则、空值和三种状态分离 |
| 字段可追踪到类型 | 第 8–9 节；配置、上下文、经验和输出都有映射 |

步骤 1–4 加入配置、领域和插件装配测试；步骤 5 加入本地持久化、候选隔离、失败经验、提交恢复与冻结版本导入测试。完整进化主循环仍待后续实现，不以存储测试或 Mock 结果宣称研究收益。
