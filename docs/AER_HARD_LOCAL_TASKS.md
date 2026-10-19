# 较难本地任务集 hard_local

hard_local 是一组用于调试真实模型进化行为的手工编写多规则任务。新增 6 个开发任务、6 个同类验证任务、4 个基础回归任务和 6 个最终测试任务，共 22 条。原有 toy_tasks 的内容、版本和 4/8/4 划分保持不变。

这些任务增加了规则依赖、边界条件和多个输出字段，实际难度取决于模型。当前已验证数据、评分与完整控制流程，尚未进行真实 Qwen 预跑，不预设它们一定导致失败或产生进化收益。

## 任务与能力

| 开发任务 | family | 规则和主要边界 |
| --- | --- | --- |
| hard-dev-001 | invoice | 取消/退货、满额折扣向下取整、折后免邮、运费税基和半分向上舍入 |
| hard-dev-002 | allocation | 禁用对象、容量上限、迭代重新分配、精确余数排序与平局处理 |
| hard-dev-003 | revisions | 先按修订号和最后出现位置解决冲突，再删除、筛选、排序和按 owner 去重 |
| hard-dev-004 | scheduling | 多人可用窗口、忙碌区间、半开边界、绝对时间网格和最早可行时间 |
| hard-dev-005 | tags | 归一化后计数与过滤，按频率/长度/字典序排序，保持输出字段对应关系 |
| hard-dev-006 | selection | 精确选择数量、预算、必选类别、冲突对、全局最大价值和多级平局规则 |

同类验证每个 family 各一条，使用不同输入及边界组合；属于同一规则族内迁移，不声称跨领域泛化。回归组是独立编写的基础整数/分数运算、字段提取和字符串排序任务，没有复用 toy_tasks 的实例。测试组每个 family 各一条，包含容量不足、空结果和无可行排程等情况。

开发 ID 为 hard-dev-001 至 hard-dev-006，同类验证为 hard-similar-001 至 hard-similar-006，回归验证为 hard-regression-001 至 hard-regression-004，最终测试为 hard-test-001 至 hard-test-006。划分提前固定，验证集不随候选效果改变。

## 运行

复用项目根目录 .env 中的 QWEN_API_KEY、QWEN_BASE_URL、QWEN_MODEL。新增两份配置采用相同初始 Prompt、模型、工具和 Agent 参数：

- experiments/configs/v0_1_qwen_hard_local_no_evolution.yaml：仅初始执行与评分，便于预跑。
- experiments/configs/v0_1_qwen_hard_local.yaml：Fixed Prompt 进化与完整闭环。

查看公开开发任务：

```powershell
.\.venv\Scripts\python.exe main.py inspect-benchmark experiments/configs/v0_1_qwen_hard_local.yaml
```

先检查初始表现：

```powershell
.\.venv\Scripts\python.exe main.py run-task experiments/configs/v0_1_qwen_hard_local_no_evolution.yaml --task-id hard-dev-001 --output hard-baseline-001.json
```

运行允许进化的完整闭环：

```powershell
.\.venv\Scripts\python.exe main.py run-task experiments/configs/v0_1_qwen_hard_local.yaml --task-id hard-dev-001 --output hard-evolution-001.json
```

成功时仍会在初始执行后停止。可更换其他 hard-dev ID；若全部初始成功，需要继续校准任务难度，不把初始成功作为进化收益。若生成或模型调用发生异常，查看 error 和 stop_reason，不将基础设施失败当成能力失败。

完整预算限制按既有 MVP 约定暂缓。默认一个候选、10 个独立验证任务，第一次完整配对验证需要 22 次 Agent 任务执行（旧/新各含 Local 和 10 个独立任务），接受后另有提交后复测。模型调用次数取决于各次 Agent 执行的工具交互步骤。未进化基线通常成本更低，可先用于难度预跑。

测试集继续遵循冻结 Snapshot、关闭进化的约定，不把其结果反馈给 Evolver。

## 数据与评分

公开输入位于 plugins/benchmarks/hard_local/tasks.json；答案仅位于独立的 scoring_specs.json。Agent 只接收 TaskView 中的 instruction、data、output_format 和公开元数据。family 仅标识任务能力，不含评分答案。

全部约束都写在公开题目中，包括大小写、排序、平局、空结果、金额单位、舍入和无解约定。多规则任务使用精确结构化答案评分；基础算术使用公开容差为 0 的数值评分。每个任务输出 0/1 分，不通过 LLM 裁判评分，也不要求模型解释与标准推理文本一致。

沿用已有 deterministic_evaluator 及 toy.scoring.v1 的评分协议版本；任务内容版本为 hard-local.v0.1.0，benchmark_id 为 hard_local。数据加载检查公开/私有 benchmark 身份、版本、唯一任务/来源、评分规格一一对应、组划分和输出类型。

本次仅将本地数据加载和确定性评分的 benchmark 身份处理推广到新插件，仍使用现有公开输入结构。SWE-bench 等代码任务的仓库环境和工具适配不在本次实现范围内。

## 验证方式

测试通过手工核对的固定答案检查开发任务及关键边界；测试侧另用整数/Fraction 运算、区间枚举和穷举组合检查数据标签。参考解法仅在 tests/benchmarks/hard_reference.py，不由运行时模型 Provider 或 Agent 调用。

现有 scripted_model 只用于原有简单任务和工程模拟，没有新增 hard_local 专用解题逻辑。新任务的完整闭环测试使用受控模型 fixture 验证接口、私有信息隔离、成本和提交行为，不作为实际模型能力或收益结果。

打包资源通过 pyproject.toml 显式包含，安装后可从仓库目录外加载任务。
