# AER 加权接受策略：g004-r010

本协议复用 `weighted_improvement` 插件，算法版本仍为 `v0.1`。`g004-r010` 是本文的实验参数标签，
不是新增的插件 ID。当前 HotpotQA 三轮 baseline 使用它；其他 Prompt、Memory、Tool、Skill baseline
可以通过相同的 `validation` 配置复用。旧实验的配置与策略默认值不变。

## 评分、诊断与成功

- 评分来自 benchmark 自带的评估器；HotpotQA 使用官方 Joint F1，范围为 `[0, 1]`。
- 成功由 benchmark 的成功标准决定；HotpotQA 使用 Joint EM = 1。
- LLM 诊断提供原因和改进建议，不参与分数计算，不替代成功判断。
- GEPA 内部候选搜索继续使用相同 benchmark 分数；AER 加权策略负责外层验收。

## 接受条件

每轮比较**候选版本**与**本轮开始的稳定版本**在同一组任务上的成绩。
设当前任务、相似任务组、回归任务组的分数差分别为 `Δcurrent`、`Δsimilar`、`Δregression`：

```text
G = 0.50 × Δcurrent + 0.25 × Δsimilar + 0.25 × Δregression
接受条件：验证完整，G >= 0.04，且 Δregression >= -0.10
```

两个边界均包含等号；计算比较沿用 Decimal。`0.10` 是绝对分数下降量，不是相对下降 10%。
如稳定版本回归分数为 0.80，候选至少为 0.70，且加权收益同时达标，才可接受。
恰好 `Δcurrent=0.13`、`Δsimilar=0`、`Δregression=-0.10` 时，`G=0.04`，可以接受。

回归上限是**逐轮**约束，不是相对初始版本的累计约束。多次接受可能累积超过 0.10 的下降；
初始/最终回归变化另由留出报告记录。当前不增加“当前任务不得下降”或“成功额外加分”的规则，
因此接受与当前任务成功是不同判断。回归保护比较的是组平均分；当前配置每组只有一道题。

拒绝理由按已有插件顺序记录：验证错误/不完整或缺少当前任务指标、`no_improvement`（G ≤ 0）、
`insufficient_improvement`（0 < G < 0.04）、`regression_guard`（回归下降超过 0.10）。
同时违反收益与回归条件时，主理由优先记录收益不达标，完整分数组件仍保留。

## 可复用配置

把以下字段写入其他实验已有的 `validation` 区块；保持各 benchmark 的验证组配置：

```yaml
acceptance_policy: weighted_improvement
policy_version: v0.1
weights:
  current_task: 0.50
  similar: 0.25
  regression: 0.25
min_improvement: 0.04
max_regression_drop: 0.10
```

复用前确认其他 benchmark 的分数方向、尺度与分组语义适合这些参数。

## 版本选择与记录

最多执行三轮外层进化。初始任务成功时使用初始版本直接结束；候选接受后采用新稳定版本，
若它在当前任务上的已验证结果成功则提前结束，否则进入下一轮。候选拒绝或没有候选时，
保留原稳定版本继续下一轮。到第三轮仍未成功时，保留最后一次成功提交的版本；没有提交则保留初始版本。
提交后的成功判断复用已验证的执行结果，不额外执行当前题或调用 LLM 判断成功。
执行、诊断、生成、验证等错误仍按原有错误规则停止该任务，不强制凑满三轮。

每次 `run-benchmark` 保存：

- `baseline_protocol.json`：启动时确定的接受参数、外层轮数、提前停止设置、路由、组件配置、
  benchmark/scorer 版本与并发数。用于后续复现实验；文件不是可直接传入 CLI 的完整实验配置。
- `baseline_summary.json`：同样保存 `acceptance`、`evolution`、`limits` 和 `workers`；
  `tasks[].rounds[].candidates[].acceptance` 记录分数组件差值、加权贡献、总收益、是否接受与理由。
- 每轮 `record.json`：保留具体候选、验证结果和提交结果，支持复核接受判断。

`acceptance.reward` / `weighted_gain.total` 是加权收益；`delta_score` 是留出组综合分数差，
不能当作加权收益。最终成功率使用最终稳定版本的 benchmark 结果。

三路并行时，每个任务条目的 `run_root` 指向它的子运行目录；条目中的 `record`、`result_ref`
与其他工件引用按该子目录解析。每个子运行也保存自己的协议与汇总；父目录保存整批协议和总汇总。
