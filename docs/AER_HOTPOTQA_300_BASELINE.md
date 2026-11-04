# HotpotQA：300 个独立任务的 Fixed-GEPA baseline

本批使用 HotpotQA distractor。配置为
`experiments/configs/v0_1_codex_hotpotqa_gepa_300.yaml`，数据清单为
`experiments/data/hotpotqa_300_manifest.json`。
原有四题配置及清单保留，可继续复现此前实验。

## 数据与选题标准

| 角色 | 数量 | 是否进入 GEPA 内部搜索 |
| --- | ---: | --- |
| 当前任务 | 300 | 是，每次只使用当前这一题 |
| 相似任务 | 300，每个当前任务 1 道 | 否，用于外层验收与报告 |
| 回归任务 | 300，每个当前任务 1 道 | 否，用于外层验收与报告 |

900 道题全部来自本地官方 train 副本。任务 ID 和规范化题面均全局不重复：
当前任务与留出任务不重叠，不同当前任务也不复用留出题。本批没有额外的最终测试集。

数据准备先检查原始结构和证据引用完整性。90,447 道 train 题中有 22 道的证据编号
不在给定上下文内，排除后剩余 90,425 道；不修正原始答案或证据。
检查涉及证据引用是否存在，但不按答案内容、执行成绩或成功/失败筛题。
随后只使用 `_id`、`question`、`type`、`level` 进行选择。

**当前任务：** 种子 42，按 `sha256(seed:task_id)` 排序，在 bridge 和 comparison
中交替取题，各 150 道。某道题必须有未占用且满足规则的相似题才能成为当前任务；
本次跳过了 9 道暂时找不到匹配的候选当前任务，清单记录其 ID 和原因。
这些题仍可按既定规则成为回归题。这是按类型均衡、受匹配条件约束的样本，
不声称是原始 benchmark 分布的无偏随机样本。

**相似任务必须同时满足：**

1. 官方推理类型相同：bridge 对 bridge，comparison 对 comparison。
2. 依据问句识别的类型相同，识别规则如下表。
3. 官方难度 `level` 相同：easy、medium 或 hard。
4. 问句 TF-IDF 余弦相似度至少为 0.10。在尚未使用的合格题中选得分最高者；
   同分按固定哈希顺序决定。

| 问句类型 | 固定规则，按表中顺序优先匹配 |
| --- | --- |
| yes_no | 以 is/are/was/were/do/does/did/has/have/had/can/could/will/would/should 开头 |
| location | 含 where、what city/country/state/location、which city/country/state |
| time | 含 when、what year/date、which year |
| quantity | 含 how many/much/long/old/tall/far |
| person | 含 who/whom/whose |
| reason | 含 why |
| entity_or_other | 其余问句 |

这里只依据问句措辞，不读取标准答案来推断答案类型。
TF-IDF 使用 NFKC、casefold、单词分词和代码中固定的停用词表；词频权重为
`1 + ln(tf)`，IDF 为 `1 + ln((N+1)/(df+1))`，在去重后的合法 train 题面上计算，
经 L2 归一化后计算余弦。它是结构约束下的文本相似度，不是模型对语义等价的判断，
也不要求相同实体或主题。本批相似度最小值约 0.113，中位数约 0.291。

**回归任务必须同时满足：**

1. 与当前任务的推理类型不同：bridge 对 comparison，反之亦然。
2. 问句类型不同。
3. 官方难度相同。
4. 从满足规则且未使用的题目中按固定哈希顺序选择，不依据模型成绩。

回归题用来检查其他任务上的能力变化，不要求初始 Prompt 必须答对。
报告单独统计“初始成功、最终失败”。只有一道回归题时，它只提供一个观测，
不能证明所有其他能力均未退化。

规则、源文件哈希、每组映射、特征、文本相似度、排除记录均写入清单。
`hotpotqa_300_selection_report.json` 提供分布、样例和本地加载校验结果。
启动会再次核验全局互斥及实际源题的类型、问句类型、难度，防止清单误配。

## 运行协议和指标

- 300 次 independent 实验，从相同初始 Prompt 开始，互不继承修改；不是训练一个通用 Prompt。
- fixed_router 只选择 Prompt；GEPA 内部搜索照常，每题最多 9 次内部试跑、2 次反思迭代，单次评估采样 3 次。
- 每题一轮外层进化。评分使用官方 HotpotQA 指标；Joint EM 为成功标准，Joint F1 为优化分数。
  LLM 用于失败诊断和 GEPA 反思，不用于打分。
- 外层权重保持：当前任务 0.50、相似任务 0.25、回归任务 0.25；最低加权增益 0.01，
  回归分数不允许下降。相似题可以下降，只要总收益及回归保护条件仍满足。
- `--report-holdouts` 在每次进化结束后记录初始与**最终稳定版本**的留出表现。
  通过、拒绝、无候选、初始已经成功的任务均覆盖；不会只统计提交成功的候选。
  已有同版本、同任务、同种子、同评分条件的完整结果从缓存复用，缺失结果才执行。
  最终版本未变时复用初始观察，不人为重复采样。该报告不触发新的进化或提交。
- 额外报告执行计入总 Token/耗时；缓存复用不重复计费。留出执行错误单独记录，
  不丢掉已经完成的当前任务成绩。

`baseline_summary.json` 中主要看：

| 路径 | 内容 |
| --- | --- |
| `aggregate.current_task_initial_vs_final` | 当前题初始/最终成功率、F1、分数增量、四类成功状态转换 |
| `aggregate.holdouts_initial_vs_final.similar` | 相似题相同指标 |
| `aggregate.holdouts_initial_vs_final.regression` | 回归题相同指标及初始成功题中的失败比例 |
| `tasks[].rounds[].candidates` | 候选表现、加权接受理由和是否提交 |
| `tasks[].holdout_reporting` | 最终保留版本的逐题留出指标、错误、缓存命中及完整证据引用 |
| `usage` | 进化和额外报告合计实际消耗 |

`initial_success_rate_all` 和 `final_success_rate_all` 以所有已完成处理的任务/已分配留出题
为分母，缺失评分不算成功。F1 均值仅统计已评分样本，并明确缺失数量；配对增量只使用
两侧都有评分的题。运行中 `pending_tasks` 显示尚未完成的当前任务；完整运行的当前题
和每类留出题分母各为 300。四类转换为失败→成功、成功→失败、均成功、均失败。
当前题初始/最终比较沿用框架已有执行及已验收结果，不额外重跑当前题。

相似与回归题参与验收，所以这些指标属于运行时验收表现，不是未参与选择的最终泛化测试。

## 指令

在仓库根目录执行。本机完整数据已在 `.aer-cache/hotpotqa/`，清单已生成，无需再次下载。
迁移到新机器时，可用 `.venv/Scripts/python.exe -m experiments.prepare_hotpotqa --download-only`
恢复固定版本的数据源。

正式运行 300 个当前任务及对应留出题：

```powershell
.venv\Scripts\python.exe main.py run-benchmark experiments/configs/v0_1_codex_hotpotqa_gepa_300.yaml --report-holdouts --run-id hotpotqa-fixed-gepa-300-v1
```

可选的单题机制检查：

```powershell
.venv\Scripts\python.exe main.py run-benchmark experiments/configs/v0_1_codex_hotpotqa_gepa_300.yaml --report-holdouts --limit 1 --run-id hotpotqa-fixed-gepa-300-smoke
```

每次使用新的 run ID。报告位于 `runs/<run-id>/baseline_summary.json`，边运行边保存。
本批修改完成时没有启动真实 300 题运行。当前批量入口不支持断点续跑；全局累计 Token/
时间预算仍是原有未强制执行项，配置值不能当作费用硬上限；GEPA 内部调用次数和时间限制保持。

复现选题到一个新的文件（已有文件不会覆盖）：

```powershell
.venv\Scripts\python.exe -m experiments.prepare_hotpotqa --tasks 300 --similar 1 --regression 1 --test 0 --selection exclusive --seed 42 --output experiments/data/hotpotqa_300_reproduced.json
```

准备命令的默认参数已改为上述 300/1/1、无额外测试题和 exclusive 选题。
旧版共享留出池的生成方式仍可用 `--selection legacy-shared` 显式选择。
