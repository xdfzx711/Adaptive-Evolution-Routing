# SWE-bench 静态评估接入

本配置使用真实 SWE-bench Lite 问题与修复前仓库，通过实际代码补丁进行静态评分。不使用 Docker、不安装项目依赖、不运行测试，也不调用评估大模型。Codex 执行任务；Qwen 仍负责诊断、路由与进化。

## 运行

沿用已有 Codex 登录状态和 `.env` 中的 Qwen 配置，系统需能运行 `git`。仓库首次使用时获取指定基准提交，保存在项目 `.aer-cache/repos`，后续复用。

```powershell
.\.venv\Scripts\python.exe main.py run-task experiments/configs/v0_1_codex_swebench_static.yaml --task-id psf__requests-1963 --output swebench-result.json
```

日志仍位于本次 `runs/run-<时间>/evolution.txt`，每轮详情仍为 `rounds/<任务会话>/round-001/record.json`，补丁工件保存在对应轮次的 artifacts 下；不增加另一套日志目录。

仅检查公开任务和配置，不调用任何模型或下载仓库：

```powershell
.\.venv\Scripts\python.exe main.py inspect-benchmark experiments/configs/v0_1_codex_swebench_static.yaml
```

## 三个实例的用途

| 实例 | AER 角色 |
| --- | --- |
| psf__requests-1963 | 当前开发任务 |
| psf__requests-2148 | 同类独立验证 |
| psf__requests-2317 | 回归独立验证 |

共三个不同问题，均来自 psf/requests。配置默认最多进化一轮，每轮一个候选；当前任务初始静态评分通过时直接结束，否则进入诊断、路由和候选验证。验证会执行上述三个问题的旧版与候选，已有同条件观察由现有缓存复用。因此三个实例不表示仅调用三次 Codex。

这些记录原属 SWE-bench Lite 的 test 数据；此处显式重新分配为 AER 的工程调试角色，不作为独立最终测试。配置的 test 划分为空。小样本结果不等同于官方 SWE-bench 成绩。

轮数仍来自配置，也可以通过命令行覆盖：

```powershell
.\.venv\Scripts\python.exe main.py run-task experiments/configs/v0_1_codex_swebench_static.yaml --task-id psf__requests-1963 --set limits.max_evolution_rounds=3 --output swebench-result.json
```

## 三个主任务、每题最多五轮

扩展配置为 `experiments/configs/v0_1_codex_swebench_three_tasks.yaml`，对应数据为 `experiments/data/swebench_lite_three_tasks.json`。共 9 个不同的 SWE-bench Lite 实例，每个主任务关联同仓库的一个同类验证问题和一个回归验证问题：

| 主任务 | 内容 | 同类验证 | 回归验证 |
| --- | --- | --- | --- |
| psf__requests-1963 | 重定向链中的请求方法处理 | psf__requests-2148 | psf__requests-2317 |
| pytest-dev__pytest-7373 | skipif/xfail 字符串条件的缓存 | pytest-dev__pytest-11143 | pytest-dev__pytest-7220 |
| pylint-dev__pylint-6506 | 无法识别的命令行选项触发 traceback | pylint-dev__pylint-7228 | pylint-dev__pylint-7993 |

这里的“同类”按同一仓库的代码修复任务划分；“回归”是该仓库另一项独立行为检查。它们是工程调试分组，未经过故障根因相似性标注。每轮只评估当前主任务及它的两个配套验证问题，不执行另外两组。评分、接受规则及静态评估方式沿用原配置。

每题独立从初始 Prompt 和 `gpt-5.6-sol` 开始，Router 可以选择进化 Prompt 或执行模型。`limits.max_evolution_rounds: 5` 是上限；成功、验证异常等现有停止条件仍会提前结束。不会强制凑满五轮。轮数可继续通过 `--set limits.max_evolution_rounds=...` 覆盖。

在项目根目录执行下面的 PowerShell 命令，按顺序运行三题。每题生成独立运行目录和结果文件，失败的一题不会阻止后续题目启动：

```powershell
foreach ($taskId in @('psf__requests-1963', 'pytest-dev__pytest-7373', 'pylint-dev__pylint-6506')) {
    .\.venv\Scripts\python.exe main.py run-task experiments/configs/v0_1_codex_swebench_three_tasks.yaml --task-id $taskId --output "swebench-$taskId.json"
}
```

也可以使用相同配置加一个 `--task-id` 单独运行。每题日志位于控制台打印的 `runs/run-<时间>/evolution.txt`，分轮记录与代码补丁按现有 round 目录保存。

仅检查每题的验证集对应关系，不调用模型或下载代码仓库：

```powershell
.\.venv\Scripts\python.exe main.py inspect-benchmark experiments/configs/v0_1_codex_swebench_three_tasks.yaml
```

输出中的 `validation_tasks_by_execution_task` 列出每个主任务实际使用的同类与回归验证问题。三个主任务最多启动 15 轮进化；配对验证包含额外的 Codex 调用，模型消耗会高于原来的一题一轮配置。资源预算硬限制仍按当前 MVP 约定暂未启用。

## 四个插件

| 类别 | 首版实现 | 配置位置 |
| --- | --- | --- |
| benchmark | swebench_adapter | benchmark.plugin / benchmark.config |
| workspace_provider | local_repo_workspace | agent.config.workspace |
| result_collector | git_patch_collector | agent.config.collector |
| evaluator | static_patch_evaluator | assessment.evaluator / assessment.evaluator_config |

统一契约位于 `aer_core/contracts/benchmark.py`、`workspace.py`、`evaluator.py`。启动层注册、检查配置并注入依赖；主控制器仍只执行 Agent 和 Evaluator 接口。现有 JSON 任务默认使用 empty_workspace / json_result_collector，可继续运行。

WorkspaceProvider.prepare 返回 WorkspaceHandle，包含实际目录、收集补丁使用的本地基准提交和任务的原始基准提交；release 负责释放环境。首版保留新建工作区。ResultCollector.schema 声明可选的模型输出格式，collect 返回 CollectedResult，包括输出、工件引用和状态，不进行评分。

## 工作区与产物

每次执行向独立目录导出 base_commit 对应的源码，再创建只有该初始源码的新 Git 历史。Agent 看不到缓存、后续提交和远程地址，也不会获得参考补丁或评分专用测试。

工作区保留指向仓库内部普通文件的原始相对符号链接及 Git 的 `120000` 文件模式，以支持 Pylint 等仓库中的链接样例。Windows 需允许当前用户创建符号链接；目标越出工作区、悬空链接及其他不支持的条目会在准备阶段报告错误。

Codex 可以用普通文字总结。框架相对于保留的基准提交收集实际修改，包括已暂存修改、删除和非忽略的新文件；即使任务中新增提交，补丁仍以最初基准为参照。补丁保存在 Artifact Store，并通过 ExecutionResult.artifact_refs 关联。

只对可检查的修改后 `.py` 文件做 AST 解析，不执行代码；原始文件本身使用当前 Python 无法解析的历史语法时跳过该文件，记录实际检查数量。非文本补丁作为不支持的静态产物处理。

## 静态评分

解析补丁中的文件路径和增加/删除行，忽略 hunk 行号及上下文行。保留行缩进和字符串内容，忽略行尾空白。重复的修改行按次数计数。

令 C、G 为候选与参考的 `(文件, 增删类型, 行内容)` 多重集，F_C、F_G 为修改文件集合：

```text
line_f1 = 2 × multiset_intersection(C, G) / (|C| + |G|)
file_f1 = 2 × |F_C ∩ F_G| / (|F_C| + |F_G|)
score   = changed_lines_weight × line_f1 + (1 − changed_lines_weight) × file_f1
```

默认 changed_lines_weight=0.8，success_threshold=1.0。空补丁、不支持或结构不完整的补丁、已检查文件存在语法错误时得 0 分。其他补丁输出连续分数，达到配置阈值时 success=true。分数完全一致只代表静态比较达到阈值；不同位置的相同行改动也可能得到相同分数，语义等价的其他解法也可能得低分。

EvaluationResult.metrics 保存 changed_line_f1、changed_file_f1、patch_valid、syntax_valid、tests_executed=0。接受策略继续比较验证集静态均分及回归分数。Router 与 Evolver 只收到公开代码产物、过滤反馈和验证摘要，不收到参考补丁。

后续轮次的历史尝试会附带候选变更摘要、生成理由、分数变化、接受规则原因和提交状态；本轮 Router 的目标及理由也会传给 Evolver 模型。Prompt 历史只保留有限的文本差异，模型选择历史记录旧模型与新模型。`evolution.history_max_attempts` 控制最多保留多少次历史尝试，`evolution.history_max_text_chars` 限制每条变更摘要和候选理由的长度；当前三题配置分别为 5 和 320。旧运行记录没有候选变更字段时仍可加载，相关摘要为空。

## 数据与扩展

随项目保存的 `experiments/data/swebench_lite_smoke.json` 仅含选定三个原始实例所需字段与来源信息，不会在启动时自动加载完整数据集。来源：[SWE-bench Lite 数据集](https://huggingface.co/datasets/princeton-nlp/SWE-bench_Lite)。任务代码、问题与补丁应按其原始来源的许可使用。

增加任务时，在本地数据的 instances 列表中加入 SWE-bench 行，并在 benchmark.config.roles 显式分配 dev / similar / regression / test。任务唯一性、划分互斥和启用进化时两个验证组非空仍由配置与 Benchmark 共同检查。

当不同主任务需要不同验证集时，使用 `benchmark.config.validation_sets`：以主任务 ID 为键，分别列出 `similar` 和 `regression` 实例 ID。配置必须覆盖全部 dev 任务，两组均非空、组内无重复，引用实例的 role 必须与组名一致；主任务和未知 ID 都不能作为验证实例。未提供该配置时继续使用共享验证集，原冒烟配置保持原有行为。

按任务选择验证集的通用可选契约为 `TaskValidationProvider.validation_tasks(task)`。插件返回验证划分中的任务子集，启动层检查并分组，Harness 按这一子集配对执行。不实现该契约的 Benchmark 插件继续使用共享验证集；主控制器无需修改。

后续可注册其他 ResultCollector 或 Evaluator（包括测试执行评分），无需将 SWE-bench 的问题字段、Git 或评分逻辑放进主控制器。
