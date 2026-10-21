# AER 单任务自进化主控制器

同步 Orchestrator 把任务执行、失败诊断、目标路由、候选生成、配对验证、接受选择和实际提交接成一个任务 episode。2026-10-19 起采用单次观察语义：相同版本/任务/种子/评测条件复用已有执行与评分，提交后采用候选验证结果。

## 使用入口

```powershell
.\.venv\Scripts\aer.exe run-task experiments/configs/v0_1_fixed_prompt.yaml --task-id dev-001 --run-id loop-demo --output run-result.json
```

同样支持 v0_1_no_evolution.yaml、v0_1_manual_prompt.yaml、第二个 Agent 及 v0_1_qwen_fixed_prompt.yaml。Qwen 的凭据、URL 和模型名继续从 .env 读取。

run-task 为指定任务创建新 episode 并运行完整闭环，最终保存 RunResult。正常成功或达到配置停止边界时退出码为 0，组件异常或显式预算中断为 2。正常结束不一定表示任务成功，应查看 stop_reason 和 final_evaluation.success。

execute-task、propose-prompt 和 validate-proposal 保留为独立调试入口。run-task 复用它们的公共执行、生成和验证实现，在进程内传递类型化记录，不通过子进程拼接 CLI。

完整预算账本、episode 资源硬上限和连续无提交停止条件继续暂缓。模型/工具单次超时、Agent 步骤上限、进化轮数上限和 Manual 计划限制生效。

## 分层与职责

| 位置 | 职责 |
| --- | --- |
| aer_core/orchestrator.py | 单任务状态、停止检查、组件调用、提交/恢复、验证结果承接与 RunResult |
| aer_core/contracts/orchestration.py | ProposalService 和 CandidateValidationService 接口 |
| aer_core/task_executor.py | 指定任务、Snapshot、RunContext 的独立执行和外部评分 |
| experiments/orchestration.py | 装配、版本作用域、Benchmark 公开上下文策略、CLI 记录 |
| experiments/evolution.py / validation.py | 可共享 PreparedExecution 的生成/验证服务 |

核心控制器只依赖配置、领域类型和接口实例，不导入 plugins、experiments、模型 SDK 或 .env。具体上下文权限和本地 Benchmark 结构在启动层提供。候选通过通用 artifact_updates 创建 Snapshot，控制器没有 Prompt 替换逻辑。

## 执行顺序

1. 创建 episode，取得其起始稳定 Snapshot。
2. 新会话执行当前任务并外部评分。成功立即停止，执行/评分异常记录后停止。
3. 失败时依次检查 disabled、配置轮数、Manual 下一轮计划、可行目标。
4. 确认可进入下一轮后才诊断；诊断输入经过 ContextBuilder 过滤。稳定版本和执行记录不变时复用对应诊断。
5. 调用生成服务，在 Router 前保存 attempt_started 并占用一轮。路由/生成错误停止；空候选继续检查下一轮。
6. 构造冻结候选 Snapshot，配对验证并接受选择。普通无收益或回归拒绝继续下一轮；任一候选验证不完整或基础设施异常，整批不提交，以 validation_error 停止。
7. 提交唯一选中的候选，使用其确切候选 Snapshot、完整验证报告和接受结果，不重新生成工件。
8. 提交前检查选中候选具有完整、任务/版本/seed 一致的当前任务验证结果。提交成功后直接采用该执行与评分，新增调用与成本为 0。
9. 已采用结果为失败时保留新稳定版本，并据此诊断和进入下一轮；成功则结束。后续旧版验证复用该版本已保存的逐题结果。

初始执行和结果复用不增加进化轮数；空候选、普通拒绝、Router/Evolver 错误和验证中断占用已经开始的轮次。多个候选仍是一轮。轮数来自配置，不固定在循环中。

候选被拒绝时稳定版本没有改变，复用最近的当前任务执行/评分，不额外重跑相同版本。下一轮会重新构建包含刚完成经验的上下文。

## 提交及最终评分

accepted、selected 与 committed 分开保存。Version Store 核对父版本与完整验证证据，原子切换稳定指针，控制器追加真实候选提交结果。

普通提交失败保存 commit_failed 并停止，旧版本保持稳定。已发布指针但回执/事件未完成时，通过 Version Store 的持久化恢复核对并补齐。若回执持续失败，保留已经发布的新版本、对应候选验证结果及 committed=true 的事实，以 commit_error 停止。

任务结果始终区分：

- 最终稳定版本已经完成当前任务评分：final_evaluation_status=evaluated，final_execution/final_evaluation 属于该版本。
- 选中候选缺少完整、匹配的当前任务结果：提交前以验证错误终止。新版本实际提交后，final_* 采用该版本已验证的执行与评分，包括发布后回执持续失败的情况。

不能将旧版本分数绑定到新稳定版本。当前任务验证失败也不自动撤销通过独立验证的提交。

## 身份、成本和经验

一个 episode 保持同一 run_id/episode_id。首次实际执行创建独立会话；缓存复用保留原始执行的 operation_id，reuse_operation_id 标记本次读取所属操作，不伪造新的模型轨迹。相同 run 内不同 episode 也可以复用满足条件的已保存观察。

成本分别累计当前任务执行/评分、诊断、生成尝试、验证批次；不再次累加它们内部的嵌套 usage。缓存命中不计历史调用成本，共享旧版验证只计一次。episode.elapsed_seconds 直接用单调时钟测量，未知 tokens 保留 null。Qwen 配置使用大模型诊断，成功和失败调用的实际 usage 都累计一次；稳定版本与失败执行不变时复用诊断。本地规则诊断不调用 Provider。

除了既有的尝试、候选和提交事件，还会保存 controller_execution、evolution_proposal、validation_batch 和 run_result 工件，最后追加 task_finished。RunResult.experience_refs 关联已保存的尝试、候选和提交记录，TaskFinishedEvent 自身不循环引用自己。

当前不自动续跑中断的 episode，也不自动重试组件。重复执行已使用的 episode_id 会报错；再次调用 run-task 创建新 episode。提交回执恢复是完成已发生提交的持久化步骤，不重新调用生成模型或验证。

## 独立与累计版本

每个 run 仍共享不可变工件和可审计经验，稳定指针采用作用域区分：

| task_mode | 稳定指针作用域 | 下一任务起始版本 |
| --- | --- | --- |
| independent | 每个 episode 独立 | 实验初始 Snapshot |
| continual | orchestrator/continual | 前一任务最终稳定 Snapshot |

默认存储作用域仍保存实验起始版本；查看完整任务的最终稳定版本要使用返回的 stable_scope_id 或 episodes 中的版本存储配置。不能把默认作用域指针当作某个独立任务的最终指针。

每个任务轮数重新从 0 开始。独立模式历史仅当前 episode；累计模式通过已保存 task_finished 才能看到先前完成开发任务的允许摘要。会话不跨任务保留。批量任务顺序和实验汇总由后续统一 Runner 负责。

最终测试只允许 disabled 和明确冻结的 initial_snapshot_ref；失败不会触发诊断或进化。它的反馈不能进入开发任务进化上下文。

## 输出与 Python 调用

run-task 默认显示简短进度和输出位置。运行目录中的 `evolution.txt` 用于浏览整体过程，多个 episode 顺序追加到同一文件；每个准备进入的进化轮次另用一个 `rounds/<任务名>-<会话时间>/round-001/record.json` 保存完整细节。每轮只有一个阅读 JSON，不按阶段或候选拆出更多日志，新运行不另建 `run.log`。时间采用带时区的本地时间，默认 run、episode、attempt、operation 编号也采用本地时间格式，例如 `run-2026-10-15_14-30-05_123456`。显式传入的 `--run-id` 保持原样。

分轮 JSON 展开实际允许反馈、诊断、历史摘要、路由、组件、逐题执行与评分、接受选择、提交和提交后采用的验证结果。旧版与候选均标记缓存命中，原始 usage 与 new_usage 分开。候选使用轮内序号关联，省略技术 ID。验证条件只展示实际插件和运行参数，隐藏评分规格只用于私有缓存身份。结构化组件解析成 JSON，业务输出 ID 与诊断证据序号保留。

分轮记录随阶段原子更新，是阅读视图，底层不可变工件、版本和经验负责程序加载。诊断失败也能留下对应轮次目录；这种情况尚未写入 attempt_started，RunResult.evolution_rounds_used 仍可能为 0。初始任务成功或进化关闭时，不创建空的分轮目录。报告不会反馈给 Agent/Evolver。

`evolution.txt` 固定按以下结构组织：

1. 任务与运行条件：完整公开任务输入、模型/工具、步骤与轮数限制、接受阈值、初始组件正文。
2. 初始执行：版本、外部评分、终止原因和详细记录路径。模型响应、工具调用参数与返回、解析错误详情及任务输出保存在执行工件或分轮 `record.json`，不在总览展开。
3. 每轮进化：起始稳定版本、诊断器输出、当轮过滤后的实际反馈与历史摘要、路由目标/理由、Operator，以及每个候选的组件变更和正文。Router 完整输入与原始响应保存在分轮 `record.json` 的 `routing.model_call`，总览仅提供路径，避免重复带出执行轨迹。组件表示通用，不限定为 Prompt。
4. 配对评测：当前任务、同类和回归任务逐题列出旧版/候选分数、变化、终止原因、配对种子与缓存命中。具体输出、末次响应和工具异常详情统一在分轮 `record.json` 中查看。
5. 接受与实际提交：区分 accepted、selected、committed；提交之后单独列出“提交后任务结果（复用候选验证）”，新增调用为 0。分轮 JSON 使用 post_commit_result，source=candidate_validation；历史运行中的 post_commit_execution 仍按当时的真实复测呈现。
6. 最终汇总：各候选最终状态、稳定版本、评分、停止原因、分阶段成本和总成本，以及完整运行结果的原始记录路径。逐次执行成本与任务输出保留在详细记录中。

`evolution.txt` 中当轮详情路径指向该轮 `record.json`。原始工件、验证报告、批次、缓存、候选版本和提交凭据也写入对应轮次；初始共享内容位于 rounds/initial，初始任务执行位于该任务的 initial 子目录。稳定指针位于任务目录，累计模式位于 rounds/continual。文件名使用类型和时间，episodes 清单使用 episode 时间编号。storage_index.json 保存身份与位置映射，重复内容或缓存复用已有文件；run_manifest.json 合并五份启动清单，去掉重复插件 Schema 和装配细节。旧引用通过迁移映射仍可读取。

初始版本显示为 V0，候选显示为 R1-C1 等，实际提交后才分配 V1 等稳定版本别名。差异片段及过长组件有明确节选标记和原始工件引用；短 Prompt 完整展示，组件正文上限 16000 字符。完整公开任务只展示一次；执行详情通过原始工件或分轮记录路径查看。报告不调用模型、不重新评分、不改变接受规则，且不会送入 Agent/Evolver 上下文。系统评测汇总和当轮实际提供给 Evolver 的信息分开标注。

报告在阶段记录落盘后增量写入。尚未完成的验证显示开始进度；执行被外部中断时保留已经写入的部分，没有“任务记录结束”标记表示该段尚未完成。报告渲染失败不改变进化与提交结果。原始不可变工件和经验仍是追踪依据。

控制台进度写入 stderr 并及时刷新。`--quiet` 关闭控制台进度，仍保存 `evolution.txt`。不指定 `--output` 时默认只打印报告和完整结果位置；`--json` 可恢复向 stdout 输出完整 JSON 的行为。`--output run-result.json` 继续保存完整 JSON，包含 run_root、log_path、stable_scope_id、record_ref 和 RunResult，log_path 现在指向 evolution.txt。

`experience/events.jsonl` 继续用于结构化经验。新的 task_finished 仅保存评分、轮数、停止原因、成本等 `TaskFinishedSummary` 和 `result_ref`，不重复嵌入完整执行轨迹；沿该引用仍可加载完整 RunResult。旧版包含完整 RunResult 的 task_finished 仍可读取。其他工件与验证证据保持可追踪。

已有 run-task JSON 结果也可以离线整理：从冻结配置、任务、已保存的执行/生成/验证工件和真实提交事件重建同一格式报告，不读取 .env，不装配模型，不改写原经验或工件。同一运行下已经完成的 episode 一并按执行顺序整理到一个文件；缺失内容明确标记，不补写未保存的解释。完整渲染成功后才发布文件；已有 evolution.txt 时直接返回路径，避免重复追加。历史 run.log 保持原样。

```powershell
.\.venv\Scripts\python.exe main.py summarize-run hard-result-001.json
Get-Content -Encoding UTF8 <运行目录>\evolution.txt
```

```python
from experiments.orchestration import prepare_orchestrator

prepared = prepare_orchestrator("experiments/configs/v0_1_fixed_prompt.yaml", run_id="loop-demo", console=True)
result = prepared.run_task("dev-001")
report = prepared.save_record(result)
```

需要检查装配对象时，可使用 prepared.prepare_task(task_id)，它返回本次任务的 context、scope_id、执行/生成/验证服务和 controller，再调用其 run()。首版采用同步、顺序调用。

默认 scripted_model 已能回答任务，通常初始即成功。故障与改善路径使用测试中的受控模型 fixture 验证，包括普通拒绝后改善、多轮提交后再失败、提交回执恢复、跨任务版本/历史范围、成本对账和最终版本评分归属；这些工程结果不作为研究收益证据。
