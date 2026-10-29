# Django ASGI 中间件诊断任务

## 新任务

本组数据来自官方 [SWE-bench Verified](https://huggingface.co/datasets/princeton-nlp/SWE-bench_Verified) 的 `test` 分区，由 AER 在本地划分用途：

| AER 用途 | instance_id | 问题 |
| --- | --- | --- |
| 主任务 | `django__django-13344` | ASGI 下，中间件链首个 `process_response()` 收到 coroutine，而非 HttpResponse |
| 相似任务 | `django__django-13810` | ASGI 中跳过不使用的中间件后，中间件链的同步与异步适配出现副作用 |
| 回归任务 | `django__django-11133` | HttpResponse 无法正确输出 memoryview 类型的内容 |

主任务的原始描述只有现象、复现步骤和观测输出，没有给出修复代码或应修改的方法。它需要追踪中间件初始化、继承的同步与异步适配逻辑，以及请求与响应的调用链。官方参考补丁涉及 Session、Cache、Security 三个中间件模块；对应上游记录为 [Django PR #13344](https://github.com/django/django/pull/13344)。这些是选题依据，不代表已验证当前模型会失败。

数据文件：`experiments/data/swebench_verified_django_middleware.json`。三条记录的 `instance_id`、`repo`、`base_commit`、`problem_statement`、`patch`、`version` 均保留原始值。数据包不包含 `hints_text` 和 `test_patch`。参考补丁沿用现有 Benchmark 的私有评分规格，Agent 只接收原始题面及公开仓库信息。

## 接入范围

基线配置：`experiments/configs/v0_1_codex_swebench_django_luna_low_baseline.yaml`。pytest 对应配置为 `experiments/configs/v0_1_codex_swebench_pytest_luna_low_baseline.yaml`。

- 执行模型：`gpt-6-luna`，`initial_reasoning_effort: low`。
- 外层评分：现有 `llm_patch_evaluator`。
- 进化：`disabled`，不装配 Router 或 Evolver，不进行进化轮次。
- 保留主任务、相似任务和回归任务分组，供后续插件实验复用。

使用现有 `swebench_adapter` 加载 Verified 记录，无需修改 AER 核心、接口或流程。工具生成专用诊断配置已下线，当前基线配置不会运行工具进化。

## 接入校验

配置通过 CLI 校验，三个任务的分组及公开输入检查通过。数据字段与下载的原始记录逐字段一致。在各自精确的 `base_commit` 上，三个参考补丁均可应用，修改后的 Python 文件均可静态解析。主任务、相似任务、回归任务的参考修改前后源码上下文分别为 29,011、28,778、39,476 字符，均低于现有评分器的 120,000 字符上限。检查只读取基版本源码并在临时副本中应用参考补丁，没有运行项目测试或模型推理。

## 由用户执行

先测基线，确认实际执行模型为 `gpt-6-luna / low`，再看当前语义评分是否有提升空间：

```powershell
.\.venv\Scripts\python.exe main.py execute-task experiments/configs/v0_1_codex_swebench_django_luna_low_baseline.yaml --task-id django__django-13344 --output django-luna-low-baseline.json
```

替代的工具生成算法尚未接入；确定插件后，再建立固定 tool 的进化配置。历史运行结果继续保留，但不代表当前已有可运行的工具生成链路。

每次执行都会创建新运行。Codex 当前不支持传入的 seed，因此新运行的初始输出可能与已保存基线不同。评分仍为大模型静态语义评审，不代表 SWE-bench 官方测试通过。
