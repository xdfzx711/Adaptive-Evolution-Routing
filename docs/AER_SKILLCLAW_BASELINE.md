# SkillClaw 原生 workflow baseline

这个入口让 AER 提供任务、Codex 执行环境和外部评分，SkillClaw 提供技能目录注入、会话采集、跨任务队列、总结、判断、分组、进化、发布与同步。

它与 `skillclaw_skill_evolver` 是两条独立实验路径：后者参与 AER Router 竞争并经过候选验证；此 baseline 不调用 Router、Diagnoser、配对验证、AER 接受策略或稳定版本提交。

## 启动

在项目根目录执行：

```powershell
git submodule update --init third_party/SkillClaw
.\.venv\Scripts\python.exe -m pip install -e ".[skillclaw-baseline]"
```

在项目 `.env` 中提供以下变量，或在运行环境中设置它们（环境变量优先）。密钥不要放进 YAML：

```dotenv
CODEX_BASE_URL=https://your-task-provider.example/v1
CODEX_API_KEY=your-task-provider-key
QWEN_BASE_URL=https://your-evolution-provider.example/v1
QWEN_API_KEY=your-evolution-provider-key
QWEN_MODEL=your-qwen-model
```

`CODEX_BASE_URL` 必须支持 Responses API，且支持任务配置中的模型。这个入口通过 SkillClaw 转发 Codex 请求，需要显式 API 凭据；不会自动把 Codex 的 ChatGPT 登录态转换成 API 凭据。进化使用 SkillClaw 自带的 OpenAI-compatible 客户端访问 Qwen，复用上述接口配置，不经过 AER 的 Qwen provider 包装。

先检查配置和固定源码版本，不启动服务、不调用模型：

```powershell
.\.venv\Scripts\python.exe -m experiments.baselines.skillclaw experiments/configs/v0_1_codex_skillclaw_baseline.yaml --prepare-only
```

再运行：

```powershell
.\.venv\Scripts\python.exe -m experiments.baselines.skillclaw experiments/configs/v0_1_codex_skillclaw_baseline.yaml
```

配置分为两份：

| 文件 | 用途 |
|---|---|
| `experiments/configs/v0_1_codex_skillclaw_baseline.yaml` | 初始技能库、原生解释器、两个模型接口、学习/冻结模式和触发频率 |
| `experiments/configs/v0_1_codex_skillclaw_baseline_tasks.yaml` | AER benchmark、任务顺序、Codex 模型、工作区、收集器和 evaluator |

默认执行 `hard-dev-001 → hard-dev-002 → hard-dev-003`，每完成两个任务触发一次进化，末尾不足两个的剩余会话也触发一次。可修改任务配置中的 `benchmark.task_ids`；显式列表顺序会被保留。所有相对路径按对应配置文件目录解析。

`--prepare-only` 只检查配置、源码和初始技能文件，不检查 API 连通性、原生可选依赖或实际 Codex 启动。`native_python` 可指定独立安装了原生运行依赖的 Python；默认使用当前解释器。无需安装上游的 embedding/GPU 依赖，本入口使用 `retrieval_mode=template`。

## 每次运行的流程

1. 核对未修改的 SkillClaw submodule revision `334ddf82856826b7a259470c6670fb0ef5ce2dc4`，新建隔离目录，复制初始技能库。
2. 启动独立原生代理与 workflow 进程，以 SkillHub 将初始技能种子写入本次运行专用的 local shared store。
3. AER 准备任务工作区，将当前完整技能 bundle 复制到任务内的只读目录。Codex 的模型请求指向原生代理，使用本任务唯一的 session ID。
4. SkillClaw 在 Responses instructions 中注入原生技能目录。Codex 自主选择和读取技能，继续完成任务；代理采集真实请求、回复及工具结果。
5. 任务结束时关闭原生 session，等待原生上传完成并检查队列文件。清理任务内的技能目录后，AER 才收集输出/代码补丁并评分。成功和失败任务都采集，不按照 AER 分数过滤会话。
6. 达到调度边界后调用原生 `EvolveServer.run_once()`：消费尚未处理的会话队列 → 总结 → session judge → 按实际技能引用聚合 → 改进正文/优化描述/创建技能/skip → 原生直接发布 → 原生删除已成功消费的队列项。发生原生处理错误时保留队列并使 baseline 报错。
7. 原生 SkillHub 同步发布后的完整库，保存内容摘要与文件变化。下一任务使用这一版本。先执行再学习，当前任务的得分不会因为随后技能变化而被重算。

因此这里的“候选到稳定”由 SkillClaw 的 `publish_mode=direct` 决定：它返回的有效修改经原生发布进入共享技能库，后续任务继承。AER 不用当前任务分数决定是否接受，也不会把跨任务分数差直接归因于技能改进。

跨任务积累沿用上游的 **待消费 session 队列**，并非 AER 实现的无限历史回放库。原始会话另存一份用于审计，不会重新喂回进化。每次命令创建新实验目录；当前入口没有断点续跑功能。

执行异常、评分异常、上传缺失和进化异常会导致显式错误，不冒充评分为零的任务失败；已经上传的会话保留供检查。正常评分为零的任务仍进入学习。

## Responses 兼容与读取证据

固定版本上游 `_record_responses_turn` 只保存简化文字，不保存原生 workflow 分组所需的工具调用和 `read_skills`。外围 `ResponsesCollector` 继承原生代理，在记录阶段补齐这些字段，调用上游的协议转换、工具结果整理和技能路径映射函数。它不转换发往任务模型的请求，也不替换总结、judge、聚合、进化或存储算法。

Codex 常通过 shell/code-mode 读取文件，因此适配器将工具调用 ID 与后续实际返回的内容关联。只有工具输出包含完整当前 `SKILL.md` 文本时才生成正文读取回执，并校验 SHA-256 是否属于本任务加载的技能版本。JSON 工具输出封装会被解开；仅读取附件、部分读取、截断、附带行号的读取等可能不能验证，记录为证据不足，不推断“没有读过”。

报告分别保存原生 `injected_skills`（目录注入声明）、`read_skills`、`read_receipts`、整个库的 digest 和文件摘要。目录注入不代表正文读取；完整内容暴露也不代表已证明因果改善。没有可识别读取的会话仍进入原生 no-skill 分组；创建技能不能被解释为证实原库缺少能力。

Codex 用子进程参数配置 provider 和权限，不修改用户的 `~/.codex` 或 SkillClaw 配置。任务只接触本次 staging 库，不能修改后续任务使用的共享库；收集补丁前删除 staging 目录。已有 AER MCP 技能加载在此配置中显式关闭，避免同时出现两套技能来源。provider 的 `env_key`、`http_headers` 和 HTTP transport 配置依据 [Codex 官方配置文档](https://developers.openai.com/codex/config-reference)。

## 保存结果与冻结评估

结果位于 `runs/<run-id>/skillclaw-baseline/`：

| 路径 | 内容 |
|---|---|
| `baseline.json` | 配置、任务顺序、分数、session ID、每轮原生结果、技能版本与变更 |
| `task-0001.json` 等 | AER 原始执行轨迹、独立评分、任务成本和读取回执 |
| `sessions/*.json` | 原生上传会话的原样归档；冻结模式只本地归档 |
| `shared/baseline/` | 原生待消费队列、共享技能、manifest 和版本存储 |
| `library/` | 最后同步的原生技能库 |
| `libraries/<digest>/` | 每个实际使用/发布版本的完整可复现副本 |
| `evolve_history.jsonl`、`native.log` | 原生进化历史和代理/服务日志 |

任务执行使用 Codex 报告的成本；上游 `AsyncLLMClient` 丢弃 usage，故进化成本记录为 `null`，不能当作 0，也不能声称总成本完整。当前没有额外总 token 预算控制，任务超时和单次进化周期超时分别生效。

冻结评估时复制两份配置，修改 baseline 的 `mode: frozen`、`initial_library` 指向学习运行的 `library/`（或特定 `libraries/<digest>/`），`aer_config` 指向复制的任务配置。此模式关闭 session 上传、进化和技能同步，不需要 Qwen 凭据。

如果使用最终 test split，还要按 AER 原有规则设置任务配置：`benchmark.execution_split: test`、选择 test 任务、`agent.initial_prompt: null`、`agent.initial_snapshot_ref` 使用学习报告中的 `aer_snapshot_ref`。保持相同的执行模型、Codex/workspace/collector 配置。技能版本另外由 baseline 的 `initial_library` 冻结，不在这个 AER Snapshot 内。

## 保留的上游边界

- 原生 workflow 可以 `skip`，不强制修改技能；没有读取证据不会自动遍历所有未读技能优化描述。
- 本入口固定 workflow + template + direct publication + local storage，PRM 关闭，默认 session judge 开启、skill verifier 关闭。任务边界显式关闭 session，进化按任务批次触发，而非上游默认墙钟定时触发；这些实验选择写入配置和报告。
- 当前上游 workflow 更新技能时只发布 `SKILL.md`。若原 bundle 含脚本/附件，原生同步可能移除这些文件。本入口不擅自恢复附件来改变 baseline 行为，保留旧库快照，并在 `removed_files` 中明确记录。默认项目种子技能只有 `SKILL.md`。
- 暂未启用上游 Agent workspace backend。它是另一种进化方式，应单独实验；本次不会以 AER 自建历史池或 AER 候选接受策略替代它。

## 验证

功能代码位置：

| 模块 | 本次职责 |
|---|---|
| `experiments/baselines/skillclaw.py` | 独立 CLI、任务调度、调用 AER executor/evaluator、报告与技能版本归档 |
| `experiments/baselines/skillclaw_config.py` | baseline 配置和路径解析 |
| `experiments/baselines/skillclaw_process.py` | 独立原生进程、凭据环境、控制请求和超时清理 |
| `experiments/baselines/skillclaw_worker.py` | 实例化原生 proxy/SkillHub/workflow，任务边界关闭、队列检查和同步 |
| `experiments/baselines/skillclaw_responses.py` | Responses 记录协议兼容、工具结果关联、正文读取证据 |
| `experiments/baselines/skillclaw_sessions.py` | Codex provider 参数、只读技能 staging、摘要校验与补丁收集前清理 |
| `plugins/agents/codex.py` | 可选 launch adapter；默认未注入时走原有流程。修正 `skills: null` 时能力声明与注册信息不一致的问题 |
| `plugins/evolution/skillclaw/source.py` | 提取共用源码版本/洁净度检查，保持原 AER workflow loader 行为 |

配置、可选依赖和测试分别位于 `experiments/configs/`、`pyproject.toml` 与 `tests/integration/test_skillclaw_baseline.py`。未修改 `third_party/SkillClaw` 源码。

```powershell
.\.venv\Scripts\python.exe -m unittest tests.integration.test_skillclaw_baseline -v
```

测试使用本地 HTTP 模型夹具，实际启动原生 SkillClaw 进程、实际上传/消费队列并发布/同步技能；检查跨任务继承、skip、冻结模式、篡改检测和代码补丁隔离。另有 opt-in 测试以真实 Codex CLI 连接本地 Responses 服务，验证流式代理和真实技能读取，不调用外部模型服务：

```powershell
$env:AER_TEST_REAL_CODEX = "1"
.\.venv\Scripts\python.exe -m unittest tests.integration.test_skillclaw_baseline.SkillClawBaselineTests.test_real_codex_reads_staged_skill_through_native_streaming_proxy -v
```
