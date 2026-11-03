# Adaptive Evolution Routing

ReasoningBank 固定记忆进化 baseline 见 [接入与运行说明](docs/AER_REASONINGBANK_MEMORY.md)：AER 检索、注入、评估及验收，ReasoningBank 提炼并追加候选记忆库；评估信号与诊断信号独立保存。

SkillClaw 技能进化插件、固定目标与自适应路由实验入口见 [接入说明](docs/AER_SKILLCLAW_EVOLUTION.md)。自适应配置让 Skill 与 Prompt、OS-Copilot Tool、Model 共同竞争；执行任务使用 Codex，路由和进化使用 AER 的 Qwen 接口，候选通过现有验证后晋升。另提供 Router 合成对照检查命令；现有实验配置保持不变。

独立的 [SkillClaw 原生 baseline](docs/AER_SKILLCLAW_BASELINE.md) 由 AER 执行和评估任务，SkillClaw 原生代理、队列与 workflow 负责跨任务技能学习、发布和同步，不经过 AER Router 或候选接受策略。

实验默认加载 `experiments/skills/` 技能库：ReAct 按需读取、Codex MCP 读取，SingleCall 默认预加载 `task-output-contract`。技能内容随 Snapshot 冻结；设置 `agent.config.skills: null` 可关闭。配置、隔离及测试见 [技能执行说明](docs/AER_SKILL_EXECUTION.md)。

OS-Copilot 工具演化插件与独立原生 baseline 入口见 [接入说明](docs/AER_OSCOPILOT_TOOL_EVOLUTION.md)。完整上游通过固定版本的 Git submodule 管理。

工具演化的语义评估实验使用 [OS-Copilot 语义配置](experiments/configs/v0_1_codex_swebench_oscopilot_llm_eval.yaml)，原静态评分配置保留为对照。

实验日志、轨迹和结果统一保存在 `runs/`。`run-task` 不指定 `--output` 时仍会保存完整运行记录；需要额外导出时，建议写到 `runs/<实验名>-result.json`，保持项目根目录整洁。`tests/` 保存正式回归测试和共享夹具。

AER 是一个插件驱动的 Agent 自进化实验框架。当前完成步骤 1–5、7–11：配置与装配、存储、任务评分、两个 Agent、诊断、上下文过滤、目标路由、候选生成、配对验证、接受选择、版本提交与验证结果复用。步骤 6 的完整预算管理按 MVP 范围推迟；批量实验 Runner 和实验汇总仍待实现。

## 安装与环境

需要 Python 3.11+。在项目根目录执行以下 PowerShell 命令：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
```

运行依赖是 Pydantic、PyYAML、HTTPX 和 python-dotenv，定义在 `pyproject.toml` 中。测试使用标准库 `unittest`，无需额外测试依赖。

## 加载与校验配置

```powershell
.\.venv\Scripts\aer.exe validate-config experiments/configs/v0_1_fixed_prompt.yaml
```

也可以通过 Python 入口运行：

```powershell
.\.venv\Scripts\python.exe main.py validate-config experiments/configs/v0_1_fixed_prompt.yaml
.\.venv\Scripts\python.exe -m experiments validate-config experiments/configs/v0_1_manual_prompt.yaml
```

校验成功时退出码为 0，stdout 输出 UTF-8 JSON，包含解析后配置、绝对路径及已执行的检查。配置错误退出码为 2，stderr 显示配置来源、字段路径和具体原因。

```text
Invalid configuration: .../experiment.yaml
  limits.max_evolution_rounds: Input should be greater than or equal to 0
```

可以将 JSON 保存到指定文件，父目录需要存在；输出文件不能覆盖配置、任务索引或 Snapshot 输入：

```powershell
.\.venv\Scripts\aer.exe validate-config experiments/configs/v0_1_fixed_prompt.yaml --output resolved_config.json
```

三种模板分别是 `v0_1_no_evolution.yaml`、`v0_1_fixed_prompt.yaml` 和 `v0_1_manual_prompt.yaml`。进化轮数和各资源上限均从配置读取，无固定的运行轮数。所有契约字段必须显式提供，未知字段、错误类型、重复 YAML key 和不支持的模式组合会报错；不支持 YAML merge key。`storage.root` 和 `agent.initial_snapshot_ref` 相对 YAML 所在目录解析。校验命令不创建运行目录或实例化插件。

## 任务划分检查

validate-config 默认检查 split 名称、验证分组名称不同、最终测试禁用进化等配置约束。inspect-benchmark 进一步加载打包的真实任务并检查实际任务划分。

可通过公开任务身份索引检查任务 ID、来源重复、指定任务归属及验证两组非空。仓库提供一个仅用于配置测试的索引 fixture：

```powershell
.\.venv\Scripts\aer.exe validate-config experiments/configs/v0_1_fixed_prompt.yaml --benchmark-index tests/fixtures/benchmark_index.json
```

索引格式如下，包含任务身份和公开元数据，不含题目答案或评分规格。三个 split 都需存在；开启进化时，验证集必须有同类和回归任务：

```json
{
  "benchmark_id": "toy_tasks",
  "splits": {
    "dev": [{"task_id": "dev-001", "metadata": {}}],
    "validation": [
      {"task_id": "validation-001", "metadata": {"validation_group": "similar"}},
      {"task_id": "validation-002", "metadata": {"validation_group": "regression"}}
    ],
    "test": [{"task_id": "test-001", "metadata": {}}]
  }
}
```

每个任务可选填 `source_id`，提供时一并检查是否重复。`--benchmark-index` 和 `--output` 是命令行路径，相对当前工作目录解析。以后 Benchmark Adapter 在 setup 时提供实际任务清单，并再次落实数据级检查。

`agent.initial_snapshot_ref` 非空时 validate-config 检查文件存在，init-storage 和 execute-task 进一步加载内容、工件与版本谱系，检查冻结配置兼容性。存储、Mock、Benchmark、评分、执行、路由、进化和验证层已有工厂。核心配置模块接收外部 catalog，不导入 `plugins` 的具体实现。

## 装配插件组合

```powershell
.\.venv\Scripts\aer.exe assemble-config experiments/configs/mock_fixed_a.yaml
.\.venv\Scripts\aer.exe assemble-config experiments/configs/mock_fixed_b.yaml
.\.venv\Scripts\aer.exe assemble-config experiments/configs/mock_manual.yaml
```

命令构建选中的 Agent/Router/Evolver，输出版本、配置 schema 和依赖绑定 manifest。Mock 示例保存在进程内，不创建运行目录。原始 disabled/fixed/manual 模板均已能装配真实组件；此命令不执行任务或调用模型。

插件装配支持 `--output` 和 `--benchmark-index`。Python 调用、注册方法及错误约定见 [插件注册与依赖装配](docs/AER_PLUGIN_ASSEMBLY.md)。

## 初始化本地存储

```powershell
.\.venv\Scripts\aer.exe init-storage experiments/configs/v0_1_fixed_prompt.yaml
```

命令在 storage.root 下创建独立运行目录，保存初始 Prompt、Snapshot、稳定指针和存储 manifest，输出实际位置。指定 `--run-id example-run` 可使用相同配置重新打开该运行目录。

工件和版本不可变，候选创建不改变稳定指针；提交核对已保存验证证据，并记录真实提交状态。JSONL 经验覆盖接受、拒绝、空候选和异常，支持重载与时间截止查询。目录结构、接口和恢复规则见 [本地存储](docs/AER_LOCAL_STORAGE.md)。

## Python 调用

```python
from experiments.configuration import load_experiment_config

loaded = load_experiment_config("experiments/configs/v0_1_fixed_prompt.yaml")
print(loaded.config.limits.max_evolution_rounds)
print(loaded.as_dict())
```

## 本地任务与评分

固定数据集包含 4 个开发任务、8 个独立验证任务（同类与回归各 4 个）、4 个最终测试任务，覆盖算术、信息提取和输出格式。公开输入与私有评分答案分别存储。

```powershell
.\.venv\Scripts\aer.exe inspect-benchmark experiments/configs/v0_1_fixed_prompt.yaml
.\.venv\Scripts\aer.exe score-execution experiments/configs/v0_1_fixed_prompt.yaml execution.json --operation-id score-v1
```

第二条命令接收已有的完整 ExecutionResult JSON，输出带版本和执行引用的 0/1 分数；答错属于正常评分。两条命令都不执行 Agent。规则、数据格式和 Python 入口见 [本地 Benchmark 与评分](docs/AER_LOCAL_BENCHMARK.md)。

SWE-bench 静态评估另提供 `experiments/configs/v0_1_codex_swebench_three_tasks.yaml`：三个主任务各配一个同类验证和一个回归验证问题，每题最多进化五轮、独立保留状态。顺序运行指令与配套关系见 [SWE-bench 静态评估接入](docs/AER_SWEBENCH_STATIC.md#三个主任务每题最多五轮)。

## 执行 Agent 与诊断

```powershell
.\.venv\Scripts\aer.exe execute-task experiments/configs/v0_1_no_evolution.yaml --task-id dev-001
.\.venv\Scripts\aer.exe execute-task experiments/configs/v0_1_single_call.yaml --task-id dev-001
```

两条命令分别使用 ReAct 和 Single-call Agent，返回并保存任务结果、轨迹、usage、外部评分与开发任务诊断。默认使用本地 scripted_model，tokens 是模拟成本；ReAct 算术执行包含真实的本地 calculator 调用。最终测试不会调用诊断器。用法与结果格式见 [Agent 执行与诊断](docs/AER_AGENT_EXECUTION.md)。

## 使用 Qwen API

在项目根目录 .env 中设置 QWEN_API_KEY、QWEN_BASE_URL 和 QWEN_MODEL，模板见 [.env.example](.env.example)。Qwen YAML 使用变量引用，实际模型名在启动时解析并写入 Snapshot 和运行记录。

```powershell
.\.venv\Scripts\aer.exe execute-task experiments/configs/v0_1_qwen_react.yaml --task-id dev-001
.\.venv\Scripts\aer.exe execute-task experiments/configs/v0_1_qwen_single_call.yaml --task-id dev-001
```

Qwen Provider 支持兼容 Chat Completions、工具调用、实际 token usage 和调用超时；密钥保留在运行时，.env 已忽略。配置与联调说明见 [Qwen 接入](docs/AER_QWEN_PROVIDER.md)。

Qwen 示例配置现在使用 `assessment.diagnoser: llm_diagnoser`，复用相同的 ModelProvider 和 .env 模型设置。失败诊断返回观察事实、可能原因和轨迹证据，实际调用成本计入结果；稳定版本的同一次失败执行不重复诊断。本地 scripted 示例仍保留规则诊断器，可通过配置替换。

新增 [Codex CLI 与双组件进化](docs/AER_CODEX_AGENT.md)：`codex_cli_agent` 执行任务，Qwen 负责诊断、路由和进化。Router 选择 `prompt` 或 `model`，默认执行模型为 `gpt-6.1-sol`，文本生成基准为 `.env` 中的 `QWEN_MODEL=qwen3.8-max`。旧步数进化组件已移除；ReAct 仍可作为对照插件。`evolution.txt` 展示路由决策，分轮 `record.json` 保存路由模型的实际输入与原始响应。

```powershell
.\.venv\Scripts\python.exe main.py run-task experiments/configs/v0_1_codex_router.yaml --task-id hard-dev-001 --output codex-result.json
```

命令行支持重复的 `--set 路径=YAML值`，例如 `--set agent.config.initial_model=gpt-6-sol`、`--set limits.max_evolution_rounds=5`。`evolution.stop_on_success` 默认是 `true`；设为 `false` 可在任务成功后继续进化至轮数上限，其他错误与不可行目标仍会停止运行。覆盖值先经过完整配置检查，再用于装配、Snapshot、验证及日志，原 YAML 不变。Codex 使用本机 CLI 认证，默认跳过用户 config.toml，Provider 继承方式见接入文档；Qwen 继续使用 `.env`。

## 较难本地任务

新增 hard_local：6 个多规则开发任务、6 个同类验证、4 个基础回归和 6 个最终测试任务，覆盖账单、份额分配、版本记录筛选、排程、归一化和约束组合选择。原 toy_tasks 保留。

```powershell
.\.venv\Scripts\python.exe main.py run-task experiments/configs/v0_1_qwen_hard_local_no_evolution.yaml --task-id hard-dev-001 --output hard-baseline-001.json
.\.venv\Scripts\python.exe main.py run-task experiments/configs/v0_1_qwen_hard_local.yaml --task-id hard-dev-001 --output hard-evolution-001.json
```

两份配置使用相同初始 Agent，分别预跑不进化基线与运行完整进化。实际难度和失败率需通过模型运行观察。任务划分、规则、成本与数据验证见 [较难本地任务](docs/AER_HARD_LOCAL_TASKS.md)。

## 路由与 Prompt 候选生成

```powershell
.\.venv\Scripts\aer.exe execute-task experiments/configs/v0_1_fixed_prompt.yaml --task-id dev-001 --run-id prompt-demo --output execution.json
.\.venv\Scripts\aer.exe propose-prompt experiments/configs/v0_1_fixed_prompt.yaml execution.json --run-id prompt-demo --round-id 1 --output proposal.json
```

任务失败时生成独立候选工件，任务成功则跳过。生成记录包含过滤上下文、路由、候选、成本及经验引用；候选尚未验证或提交。Qwen 对应配置为 v0_1_qwen_fixed_prompt.yaml。权限、历史范围和验收说明见 [Prompt 进化](docs/AER_PROMPT_EVOLUTION.md)。

## 配对验证与接受选择

```powershell
.\.venv\Scripts\aer.exe validate-proposal experiments/configs/v0_1_fixed_prompt.yaml proposal.json --run-id prompt-demo --output validation.json
```

对候选构建通用工件更新 Snapshot，在相同任务和种子下比较旧版与候选，保存 Local 修复、同类迁移、回归变化和接受结果。初始执行、旧版与候选共用缓存：同一版本/任务/种子/条件只执行与评分一次，成本只累计新调用。用法和数据约定见 [配对验证与接受策略](docs/AER_PAIRED_VALIDATION.md)。

## 完整单任务进化闭环

```powershell
.\.venv\Scripts\aer.exe run-task experiments/configs/v0_1_fixed_prompt.yaml --task-id dev-001 --run-id loop-demo --output run-result.json
```

Orchestrator 在一个 episode 内完成失败分析、配置轮数内的进化、候选验证和实际提交。提交后直接采用候选的当前任务验证结果，不再额外复测；成功则结束，失败继续进化。返回匹配最终稳定版本的 RunResult。控制逻辑、停止原因和 Python 入口见 [主控制器](docs/AER_ORCHESTRATOR.md)。

每次运行用 `runs/<时间编号>/evolution.txt` 浏览整体过程，每轮细节放在独立子目录中，每轮只新增一个 `record.json`，不再为诊断、生成、验证和提交分别创建阅读日志：

`evolution.txt` 保留诊断、路由理由、候选组件变更、逐题评分对比、接受与提交结果，以及分阶段成本汇总。执行轨迹、任务输出、工具异常详情和 Router 完整调用正文保存在执行工件或分轮 `record.json` 中，总览只提供详细记录路径。

```text
runs/run-<时间>/
  run_manifest.json
  storage_index.json
  evolution.txt
  episodes/episode-<时间>.json
  experience/events.jsonl
  rounds/
    initial/                   # 初始共享工件、快照与默认稳定指针
    dev-001-<会话时间>/
      stable.json
      initial/artifacts/       # 当前任务初始执行
      round-001/
        record.json
        artifacts/
        validation/            # 验证报告、批次与逐任务执行缓存
        versions/              # 候选快照、关联记录与提交凭据
      round-002/...
```

`record.json` 展开当轮任务、允许反馈、诊断、路由、候选完整内容、逐题验证、接受选择与实际提交。`post_commit_result` 明确标记 source=candidate_validation 和 reused=true，新增成本为 0；旧运行的复测记录仍按原始事实显示。候选使用轮内序号，省略技术 ID，JSON 使用 UTF-8 与两空格缩进；控制台实时显示阶段和分轮记录路径。

`evolution.txt` 中当轮生成、上下文、候选组件和验证的详情路径指向该轮 `record.json`。底层工件、执行缓存和版本记录也放在对应轮次下，文件名使用类型、任务名和时间，不使用哈希。相同内容与缓存跨轮复用原文件。`storage_index.json` 负责内部身份到物理位置的映射，哈希仅用于内容身份与校验；JSONL 经验保持每事件一行。

原来的五份启动清单合并为一份 `run_manifest.json`，按 storage、execution、evolution、validation、orchestrator 分区，保存冻结配置、实际插件与版本、任务划分、模型参数和流程规则；不重复写入插件 Schema、工厂路径和依赖装配细节。已完成的旧运行可离线迁移，先在运行目录之外保留 ZIP 备份，再移动文件并校验工件、版本和真实提交记录；旧引用通过索引继续可读，不调用模型：

```powershell
.\.venv\Scripts\python.exe main.py reorganize-run runs/run-<时间>
```

原来的 `--output run-result.json` 指令可以继续使用；不指定输出文件时默认只显示报告和完整结果位置，`--json` 输出完整 JSON，`--quiet` 隐藏实时进度。已有结果可离线生成同一个 `evolution.txt`，不重新运行模型：

```powershell
.\.venv\Scripts\python.exe main.py summarize-run hard-result-001.json
```

## 测试与后续工作

SWE-bench Lite 的三个实例现可用于 Codex 静态补丁评估，不需要容器。Benchmark、工作区、产物收集器和 Evaluator 均通过插件配置选择；运行指令、评分公式和数据角色见 [SWE-bench 静态接入](docs/AER_SWEBENCH_STATIC.md)。

三题配置现采用可替换的加权接受策略：当前任务、同类、回归收益权重为 0.5 / 0.25 / 0.25。权重、阈值及可选回归上限由配置控制，日志展开各项贡献；任务成功仍由当前任务的 Evaluator 判定。字段和兼容规则见 [加权接受策略](docs/AER_WEIGHTED_ACCEPTANCE.md)。

Prompt 可选用官方 GEPA 优化器插件，内部试跑使用独立执行实例和工作区，反思模型单独配置；路由插件和运行组合由实验配置决定。安装、字段和内部日志见 [GEPA Prompt Evolver](docs/AER_GEPA_PROMPT_EVOLUTION.md)。`v0_1_codex_swebench_jev_tools.yaml` 现选择 GEPA Prompt 与 Tool 两个目标，其他配置保留原 Operator。

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

测试覆盖配置与 CLI、领域序列化、插件组合与装配、存储恢复、任务划分与评分，以及两个 Agent、工具恢复、步骤限制、会话隔离、成本汇总和公开证据诊断。

MVP 工程验收的接口与反馈已收口：评估入口统一使用 evaluate()，公开反馈由同一个构建服务过滤；任务字段权限由 Benchmark 声明，工具字段由 ToolSpec 声明，状态与配置字段由 Agent 能力声明。通用编排不依赖 ToyInput、output_format 或 calculator。初始 Snapshot 配置由 Agent 注册回调提供；共享模型/工具按整组已选插件的依赖装配。相关契约见 [领域接口](docs/AER_DOMAIN_CONTRACTS.md) 与 [插件装配](docs/AER_PLUGIN_ASSEMBLY.md)。

领域类型、序列化示例和接口签名见 [领域模型与同步接口](docs/AER_DOMAIN_CONTRACTS.md)。下一步是统一 Runner、批量 Benchmark 和实验结果汇总。完整预算账本、资源硬上限和连续无提交终止暂缓；相关配置继续校验，但尚未执行。完整规划见 [开发指导](AER_MVP.md)，v0.1 字段与实验规则见 [范围与实验语义](docs/AER_V0_1_SPEC.md)。
