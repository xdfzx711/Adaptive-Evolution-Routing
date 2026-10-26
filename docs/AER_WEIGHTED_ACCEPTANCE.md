# 加权候选接受策略

`weighted_improvement` 是独立的 AcceptancePolicy 插件。它使用已有配对验证结果，不增加 Agent 执行，不修改 Evaluator 的评分公式。

```yaml
validation:
  plugin: paired_validation
  split: validation
  group_field: validation_group
  similar_group: similar
  regression_group: regression
  acceptance_policy: weighted_improvement
  policy_version: v0.1
  min_improvement: 0.01
  max_regression_drop: null
  weights:
    current_task: 0.5
    similar: 0.25
    regression: 0.25
```

权重必须在 `[0, 1]` 内、总和为 1，在一次运行中保持固定。配置中的三个组权重不受各组任务数量影响：先分别计算同类、回归组平均分，再加权。

每个候选与本轮起始稳定版本比较，综合收益为：

`U = current_task × 当前任务分数变化 + similar × 同类平均分变化 + regression × 回归平均分变化`

只有验证完整、当前任务指标齐全、`U > 0` 且 `U >= min_improvement` 时才接受。缺少当前任务指标时拒绝并记录 `missing_local_metrics`，不以零补齐。

`max_regression_drop: null` 关闭回归硬限制，回归下降通过负贡献扣分。设置数值时额外限制回归组平均分的下降幅度，等于上限仍允许。若设为 `0.0`，任何回归下降都会被拒绝，即使综合收益为正。

多候选优先选综合收益最高的通过候选，同分时沿用验证 tokens 和候选编号排序。接受与提交是分开的；提交会核对原始分数变化与保存的验证报告一致。

## 数据与日志

- `delta_score` 仍表示独立验证分数变化，不包含当前任务。
- `delta_current_task_score` 表示当前任务分数变化。
- `reward` 在新策略下表示综合收益 `U`。
- `weighted_gain` 保存实际权重、三项贡献和总收益。
- 历史经验、Router/Evolver 输入、每轮 `record.json` 以及 `evolution.txt` 均保留这些指标。没有额外日志文件。

旧策略 `improvement_with_regression_guard` 仍可通过配置选择，沿用独立验证收益和回归硬限制；旧经验记录仍可读取。

## 成功与结束

接受候选只表示保留该版本。提交后，仍由稳定版本在当前任务上的 `Evaluator.success` 决定是否成功结束；未成功则继续进化，直到轮数或其他停止条件。复用候选验证中的当前任务结果，不增加复测。

当前 SWE-bench 使用静态补丁评分，`success` 不代表执行过项目测试。

三个现有批量配置 `v0_1_codex_swebench_three_tasks.yaml`、`v0_1_codex_swebench_jev.yaml`、`v0_1_codex_swebench_jev_tools.yaml` 已启用上述默认权重。其他旧配置继续使用原接受策略。修改配置不会改变历史运行文件；新的接受结果需要新运行生成。
