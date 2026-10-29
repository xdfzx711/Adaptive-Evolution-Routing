# 外层 LLM 补丁评分插件

## 使用范围

`llm_patch_evaluator` 实现现有 `EvaluatorPlugin.evaluate` 接口。实验装配层选择插件，AER 的执行、路由、进化、配对验证和提交顺序保持原样；`aer_core` 无需修改。

运行配置为 `experiments/configs/v0_1_codex_swebench_gepa_llm_eval.yaml`，Router 使用 `jev_router`。静态评分对照配置为 `v0_1_codex_swebench_gepa.yaml`。两份配置只启用 GEPA prompt 进化，暂不装配工具生成算法。

| 环节 | 对照配置的评分方式 |
| --- | --- |
| 初始任务、外层当前任务评估 | `llm_patch_evaluator` |
| 外层相似任务、回归任务的稳定版本与候选版本 | 同一个 `llm_patch_evaluator` |
| GEPA 内部 prompt 试跑与选择 | 原 `static_patch_evaluator`，权重 0.8、成功阈值 1.0 |

GEPA 内层通过插件配置 `trial_evaluator` 显式固定旧评分器，避免随外层切换而继承 LLM 评分。未配置该选项时，既有实验仍继承全局 evaluator。GEPA 的搜索算法、反思模型、采样数和预算保持原配置。

## 判断依据

评分器从任务的 `base_commit` 导出独立源码副本，应用候选补丁，向 Judge 提供 issue、候选 diff、相关文件修改前后的完整源码。默认同时提供私有参考补丁，用于理解期望行为；评审指令明确排除文本相似度，允许不同但行为正确的实现。

Judge 返回结构化结论、行为分析和可核对的源码片段，由代码映射为分数：

| 结论 | 分数 | success |
| --- | --- | --- |
| `resolved`：问题得到解决，未发现具体相关回归 | 1.0 | true |
| `partial`：部分修复，仍有具体缺陷 | 0.5 | false |
| `unresolved`：问题仍在或引入阻断性缺陷 | 0.0 | false |

空补丁、无效补丁、语法错误仍直接为 0 分。缺少上下文、Judge 无法判断、响应格式错误和调用失败属于评估错误，不伪装为候选 0 分，也不回退到相似度评分。模型调用次数与已知或未知 token 消耗沿用现有 usage 记录。

这是大模型的静态语义评审，没有执行项目测试，`success=true` 也不代表 SWE-bench 官方测试通过。必要源码超出限制时会报评估错误，不会静默截断后给出分数。

参考补丁、Judge 原始响应和分析保存在 `llm_patch_review` 工件中，通过 `EvaluationResult.evidence_refs` 查阅。传给 Router/Evolver 的公开反馈只含分数和固定结论消息，保留现有私有评分信息过滤规则。

## 相似任务和回归任务

沿用原配对验证：稳定版本和候选版本分别在当前任务、相似任务、回归任务上执行并评分。Judge 不接收版本身份和任务分组。原 `improvement_with_regression_guard` 接受策略仍要求相似任务提升至少 0.01，且回归任务分数不下降；当前任务分数继续按原流程单独记录。Judge 对单个补丁的回归分析不能替代回归任务上的实际配对评估。

## 配置与运行

新配置沿用 `.env` 中的 Qwen 模型作为独立 Judge，temperature 为 0。Router 通过独立的 `jev_router` 插件调用 JEV，读取同一 `.env` 中的 `JEV_API_KEY`、`JEV_BASE_URL`、`JEV_MODEL`；接入说明见 [JEV Router](AER_JEV_ROUTER.md)。也可通过 `assessment.evaluator_config.judge_model` 指定独立 Judge provider、模型和环境变量。

主要可选项：`max_context_chars`（默认 120000，修改前后源码合计）、`max_files`（默认 16）、`max_output_tokens`（默认 2048）、`timeout_seconds`（默认 60，受全局单次模型超时的更小值约束）、`use_reference_patch`（默认 true）。评分版本包含模型运行配置与评审规则摘要，避免复用其他评分条件下的配对缓存。SWE-bench 提供的 `EvaluationSpec.scoring_method=static.patch.v1` 仍表示原参考补丁数据规格；实际评分器由插件配置和 `scorer_version=llm.patch.v1.*` 标识。

```powershell
.\.venv\Scripts\python.exe main.py validate-config experiments/configs/v0_1_codex_swebench_gepa_llm_eval.yaml
.\.venv\Scripts\python.exe main.py run-task experiments/configs/v0_1_codex_swebench_gepa_llm_eval.yaml --task-id psf__requests-1963 --output swebench-gepa-llm-eval-result.json
```

目前只允许 `prompt`，使用 GEPA，最多五轮。任务集合、初始 prompt、执行模型、GEPA 内层搜索设置、接受策略均保留。工具生成插件确定后，再通过独立插件配置接入。

GEPA 仍可能在内层相似度评分阶段淘汰语义正确的候选；外层只能评估插件实际输出的候选。两份配置使用不同 Router 插件；JEV 在只有一个可行目标时直接返回 prompt，不调用路由模型。若只比较评分标准，仍应将两组 Router 配置统一，并考虑两次运行的 LLM 随机性。

## 离线验证

新增测试使用录制的模型响应和本地 Git 仓库，覆盖替代写法、源码上下文、错误与费用记录、Judge provider 独立性、GEPA 内外评分隔离，以及相似任务提升和回归下降的接受行为。它们验证插件接入，不能证明真实 Judge 的判断准确率。

```powershell
.\.venv\Scripts\python.exe -m unittest tests.benchmarks.test_llm_patch tests.integration.test_llm_patch -v
```
