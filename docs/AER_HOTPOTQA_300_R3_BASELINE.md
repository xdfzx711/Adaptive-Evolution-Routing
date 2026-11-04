# HotpotQA 300 题：三轮、三路并行 baseline

新配置：`experiments/configs/v0_1_codex_hotpotqa_gepa_300_r3.yaml`。
数据继续使用 `experiments/data/hotpotqa_300_manifest.json`，300 道当前题各配一条相似题、一条回归题，
共 900 道互不重复的题；选择标准见 [原数据协议](AER_HOTPOTQA_300_BASELINE.md)。
原一轮配置 `v0_1_codex_hotpotqa_gepa_300.yaml` 保留用于复现旧实验。

## 运行机制

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
