# ReasoningBank 固定记忆路由 baseline

本阶段实现 AER + ReasoningBank 的固定记忆进化闭环，先使用 independent 模式。配置只开放 `memory`，不调用自主 Router，不包含 MaTTS。这是融合框架 baseline；它保留 AER 的候选验证与提交规则，不能直接称为原论文的无条件追加 baseline。

## 分工与信号

每轮执行顺序为：AER 从当前 Snapshot 的记忆库检索历史任务，注入对应记忆，执行任务并评估；诊断器解释轨迹；固定路由选择 memory；ReasoningBank 适配器提炼记忆并返回完整候选库；AER 配对验证候选并决定是否提交。

| 数据 | 来源 | 用途 |
| --- | --- | --- |
| `evaluation_signal` | AER evaluator 的已完成评估，经公开反馈过滤 | 唯一决定成功或失败模板及新 experience 的 `status` |
| `diagnosis` | AER diagnoser | 解释问题与提出假设，不能覆盖评估结论 |
| `trajectory` | AER 公开执行轨迹 | 提炼可复用的任务策略 |
| `history` | AER 过滤后的历史进化记录 | 辅助解释先前改动与验收结果 |
| `retrieved_memories` | 当前执行实际检索的记忆 | 提供已使用的历史知识 |

评估信号携带 `evaluation_id`、`execution_operation_id`、`evaluator_id` 和可用的 assessment version。LLM 评估器与 LLM 诊断器依然是两个独立角色。缺失或未完成的评估不能触发提炼。隐藏答案、测试评分规范和私有验证轨迹不传给提炼模型。

AER experience 日志仍用于审计和进化历史。Agent 检索的是 Snapshot 引用的 ReasoningBank JSONL 工件，二者不是同一个记忆库。

## 记忆格式与版本

采用上游 SWE 实现的 JSONL 格式，每行一条 experience：

```json
{"task_id":"task-1","query":"公开任务描述","memory_items":["# Memory Item 1\n## Title 检查前提\n## Description 在执行前核对约束。\n## Content 先确认输入单位和边界条件，再计算结果。"],"status":"fail"}
```

`memory_items` 是 Markdown 字符串列表，包含 Title、Description、Content；每次提炼至多三个完整记忆项。新条目只追加，不合并、删改或重排旧 experience。原库可包含上游的可选轨迹字段，读写时保留。没有可提炼信息时返回空候选。条目的 `status` 表示来源轨迹的评估结论，与候选是否被接受分开记录。

候选库与持久库是两个独立的不可变版本：

- 初始化时，将 `agent.config.memory.initial_library` 导入工件；null 表示空库。
- 提炼后创建“稳定库 + 新 experience”的完整 `.jsonl` 候选工件，通过 candidate Snapshot 引用。
- 验证使用各自 Snapshot，稳定执行只读稳定库，候选执行只读候选库。
- 通过验收并提交后，stable 指针指向候选 Snapshot。拒绝时稳定库不变，候选及其提炼记录仍保留用于检查。
- independent 为每个任务重置稳定指针；continual 在同一 run 内沿用已经提交的稳定库。重新打开同一 run 可恢复提交状态。

默认检索一个 experience，再注入其全部 memory_items。相似度使用公开 query 的 embedding 和余弦相似度。向量是进程内派生缓存，不改变原生 JSONL；重启会重新计算。成员集合始终从当前 Snapshot 读取，候选向量缓存不会让被拒绝的记忆进入稳定检索。相同 query 相似度并列时优先最新 experience，使同一任务再次提炼的记忆可以参与验证。

## 上游来源与适配范围

[论文](https://arxiv.org/abs/2509.25140)及[官方仓库](https://github.com/google-research/reasoning-bank)通过 `third_party/reasoning-bank` 子模块固定到 `ed80611788292ea739f1effd31f16c53823b8a0d`。加载时检查 commit 与提炼 prompt 文件的一致性。

适配器复用上游 `third_party/src/minisweagent/memory/instruction.py` 的成功/失败提示词及 JSONL 协议。上游函数与其执行脚本、Gemini 调用耦合，因此通过 AER ModelProvider 调用原提示词，由适配器解析完整 Markdown 记忆项并追加到新工件；没有启动上游 Agent、judge 或 MaTTS runner。提炼提示词补充了信号分工、公开证据、非编程任务以及空结果约束。Embedding 使用可配置的 OpenAI-compatible `/embeddings` 接口，而非硬编码上游服务。

## 配置与运行

先初始化源码，并按现有 Codex 配置完成 CLI 登录：

```powershell
git submodule update --init third_party/reasoning-bank
```

在根目录 `.env` 中配置提炼/诊断使用的 `QWEN_MODEL`、`QWEN_API_KEY`、`QWEN_BASE_URL`，以及检索使用的独立配置：

```dotenv
MEMORY_EMBEDDING_MODEL=<embedding 模型名>
MEMORY_EMBEDDING_API_KEY=<embedding 服务密钥>
MEMORY_EMBEDDING_BASE_URL=<兼容接口地址，通常以 /v1 结尾>
```

Embedding 端点也接受以 `/embeddings` 结尾的完整地址。可在 YAML 中修改 `api_key_env` / `base_url_env` 以复用现有环境变量；不要把密钥写入 YAML。`dimensions` 可选，只有服务支持时才设置。空库首次执行不调用 embedding，但有候选后进行验证需要该服务。`validate-config` 校验配置结构，不检查服务连通性。

hard_local 独立任务 baseline：

```powershell
.\.venv\Scripts\python.exe main.py validate-config experiments/configs/v0_1_codex_reasoningbank.yaml
.\.venv\Scripts\python.exe main.py run-task experiments/configs/v0_1_codex_reasoningbank.yaml --task-id hard-dev-001 --run-id reasoningbank-independent
```

SWE-bench 静态补丁、LLM 语义评估版本：

```powershell
.\.venv\Scripts\python.exe main.py run-task experiments/configs/v0_1_codex_swebench_reasoningbank_llm_eval.yaml --task-id psf__requests-1963 --run-id reasoningbank-swe
```

后者沿用仓库现有的静态实验条件：不执行官方测试，LLM 评估分数不能称为官方 SWE-bench resolved rate。

两份配置使用 `mode: fixed`、`fixed_target: memory`、每轮一个候选、默认 top-1 experience、最多三轮。执行模型保持固定，未改动已有自主路由配置。默认技能仍按框架配置加载；它们不参与本 baseline 的进化，如需禁用可显式设置 `agent.config.skills: null`。ReAct 和 SingleCall 也支持同一记忆读取接口。

为覆盖成功与失败轨迹，新配置显式设置 `evolution.stop_on_success: false`。这意味着成功之后仍可提炼，直到轮数或其他终止条件满足。可用 `--set evolution.stop_on_success=true` 恢复成功即停止，但首轮成功时会直接跳过提炼。

持久库仍遵循 `improvement_with_regression_guard`。成功轨迹可以生成候选，但如果验证未显示足够收益，则不会进入持久库。当前任务修复也不等于通过独立验证。这里没有将“追加记忆”自动视为“进化收益”。如果实验需要原论文式每条轨迹无条件积累，应单独设计另一套接受规则，不与此 baseline 混报。

independent 通过后，使用同一 run ID 和 continual 配置按任务顺序运行：

```powershell
.\.venv\Scripts\python.exe main.py run-task experiments/configs/v0_1_codex_reasoningbank.yaml --task-id hard-dev-001 --run-id reasoningbank-continual --set state.task_mode=continual
.\.venv\Scripts\python.exe main.py run-task experiments/configs/v0_1_codex_reasoningbank.yaml --task-id hard-dev-002 --run-id reasoningbank-continual --set state.task_mode=continual
```

## 检查结果与测试

`runs/<run-id>/evolution.txt` 展示评估来源、提炼和接受结果；`rounds/*/round-*/record.json` 包含分开的评估/诊断输入、完整提炼 request/response、候选内容及配对验证结果。工件目录中的 memory `.jsonl` 是实际记忆库；Snapshot 和稳定指针确定当前可用版本。`memory_retrieval` 轨迹事件记录工件版本、命中项、相似度及实际注入文本。

新 embedding 请求的 token 使用计入执行 usage，缓存命中不重复收费；未知使用量保持 unknown。提炼费用计入 evolution usage。全局预算强制执行仍遵循 AER 当前实现，不能将已记录成本当成完整的预算拦截能力。

不调用外部 API 的机制测试：

```powershell
.\.venv\Scripts\python.exe -m unittest tests.evolution.test_reasoningbank tests.integration.test_reasoningbank -v
.\.venv\Scripts\python.exe -m unittest discover -s tests -q
```

机制测试使用受控 embedding、提炼模型和 fake Codex，覆盖成功/失败信号分离、候选接受与拒绝、稳定库隔离、重启恢复、重复 query、真实注入与成本记录。它们验证闭环实现，不能作为真实模型 baseline 的性能结果。
