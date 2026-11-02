# SkillClaw 技能进化接入

可选插件 `skillclaw_skill_evolver` 支持固定目标和自适应路由两种运行方式，分别提供独立配置；原固定配置保持不变。Codex 负责执行任务和验证候选，`providers.model` 指定的 AER 模型负责技能进化；示例使用 Qwen，两者互相独立。SingleCall 和 preload 模式不参与本阶段进化。

## 运行

从项目根目录初始化锁定的上游源码：

```powershell
git submodule update --init third_party/SkillClaw
.\.venv\Scripts\python.exe main.py run-task experiments/configs/v0_1_codex_skillclaw.yaml --task-id <任务ID>
```

运行真实实验前，按照现有 Codex 执行配置完成 CLI 登录，并在 `.env` 配置 `QWEN_MODEL`、`QWEN_API_KEY`、`QWEN_BASE_URL`。示例选择 `hard_local` benchmark；任务 ID 来自该 benchmark。已有默认技能库仍照常加载。只有显式选择此配置或在自己的配置中选择该插件，才会执行技能进化。

核心配置如下：

```yaml
evolution:
  mode: fixed
  max_candidates_per_round: 1
routing:
  plugin: fixed_router
  fixed_target: skill
targets:
  allowed: [skill]
  default_operators:
    skill: skillclaw_skill_evolver
```

完整参数见 `experiments/configs/v0_1_codex_skillclaw.yaml`。`targets.operator_configs.skill.source_path` 可指定外部源码 checkout，路径相对于实验 YAML；同样要求锁定版本且源码未修改。wheel 不包含第三方源码，非源码安装需显式提供此路径。

## 自适应路由

`experiments/configs/v0_1_codex_skillclaw_router.yaml` 开放四个竞争方向：

| 方向 | 默认算子 | 能力 |
| --- | --- | --- |
| prompt | simple_prompt_evolver | 修改通用指令和任务策略 |
| tool | oscopilot_tool_evolver | 构造、执行、修复并导出可复用 Python 工具 |
| skill | skillclaw_skill_evolver | 修改技能正文或描述、新建技能，或说明理由后跳过 |
| model | model_selection_evolver | 在允许列表内选择任务执行模型和推理配置 |

配置使用 `llm_router`，诊断、路由及所选算子的模型调用走 AER Qwen 接口，任务和配对验证仍由 Codex 执行。每轮先筛选可行目标，再根据公开执行、技能发现/读取回执、当前组件内容、历史验证和 skip 理由选择一个方向。各方向都通过算子能力描述向 Router 声明操作范围；更换算子不需要改 Router。Tool 方向还需要已初始化的 OS-Copilot 和项目的 `oscopilot` 可选依赖，安装步骤见 [OS-Copilot 接入说明](AER_OSCOPILOT_TOOL_EVOLUTION.md)。

Router 区分可复用领域知识/环境流程与通用指令、执行工具能力。技能内容正确但 Agent 没有遵循，不能直接诊断为技能缺口；没有读取、没有发现或缺少遥测也不能互相替代。当前 SkillClaw 对未读技能的描述优化边界由能力描述明确告知 Router。`jev_router` 同样接收这些边界和此前的技能 skip 记录。

运行一轮真实任务：

```powershell
.\.venv\Scripts\python.exe main.py run-task experiments/configs/v0_1_codex_skillclaw_router.yaml `
  --task-id hard-dev-001 `
  --set limits.max_evolution_rounds=1 `
  --set evolution.stop_on_success=false
```

默认成功即停止；上述覆盖项用于成功任务的接线检查，让它也进入 Router。Router 协议仍要求选择一个可行方向，是否生成修改由所选算子决定；这不是强制选择 skill。检查 `record.json` 的 `routing.model_call`、路由理由、所选 operator 和生成结果。路由到 skill 且有候选时，再检查 `candidates[].validation.tasks[].skill_usage`，不能只看分数。

### 单独检查 Router 的识别能力

```powershell
.\.venv\Scripts\python.exe -m experiments.check_skill_routing experiments/configs/v0_1_codex_skillclaw_router.yaml
```

此命令调用配置中的真实 Router，默认是 Qwen，共六个合成对照案例：错误领域指导、正确指导被忽略、批处理工具缺口、已有技能未读、缺失遥测、成功会话。它不运行 Codex 或进化算子，不验证或提交候选。当前案例集适配上表中的能力组合；换成能力不同的 Tool 算子后，需要相应调整对照案例。

前四例比较预期方向，后两例没有唯一正确方向，留作人工检查理由是否承认证据不足、是否错误推断缺失技能。标签与评审说明只用于调用后的比较，不发送给 Router。所有证据标记为 synthetic，不计作真实任务执行记录，也不覆盖稳定库。结果保存到新运行目录的 `skill_routing_check.json`，包含逐例选择、理由、预期方向、错误、成本和原始路由回执引用。退出码 1 表示方向不匹配或调用错误；退出码 0 仍需要查看两例人工评审项。

合成对照通过只能说明这些案例上的表现，不能证明真实任务的技能缺口识别准确率或进化收益。离线测试使用预设模型响应，只验证接线、证据传递、算子隔离及报告生成；真实识别能力需要运行上面的命令，再结合真实失败轨迹评估。

## 执行链与替换边界

1. Agent 从当前稳定 Snapshot 读取冻结技能库。Codex 使用独立 MCP 服务，ReAct 使用 `read_skill` 工具，均通过已有 SkillRuntime 记录读取回执。
2. AER 构造公开的 EvolutionContext。通用 `execution_evidence()` 将它转换成 `aer.execution_evidence.v1`，不重新打开原始日志、不读取私有评分答案。输出、用量、公开轨迹、反馈和诊断分开保存。
3. SkillClaw Session 适配器将可见事件转成上游 session。每次真实执行计为一个 rollout；工具调用与返回保留各自事件身份。回执必须匹配 Snapshot、工件和正文摘要，只有确认成功的读取进入 `read_skills`；仅展示技能目录不算使用。无法定位的读取明确标为会话级证据，不编造发生顺序。服务未就绪会报错，不解释为缺少技能。
4. 原生 Summarizer 生成轨迹表示与总结；原生 Aggregator 按实际读取的技能分组；原生 Execution 对各组决定 `improve_skill`、`optimize_description`、`create_skill` 或 `skip`。没有读取技能的执行进入原生 `__no_skill__` 分支，可以新增技能或跳过。本阶段每轮适配当前执行，历史公开评估摘要作为附加证据；不伪造多条历史会话或 population 数据。
5. 模型适配器调用 AER `ModelProvider.generate()`。示例调用 Qwen，保留原生提示词、分组与解析逻辑，附加 AER 公开反馈和证据语义。上游请求的温度、输出上限与实际选项分别记录；温度遵循现有 provider 配置，输出上限遵循 AER 及插件预算。摘要是普通文本，Qwen 必须保持 `response_format: null`。
6. 输出适配器以原始冻结库为基底合成一个**完整候选库**：未修改技能原文保留，已有技能不允许重命名覆盖，描述优化保留正文，新建技能检查命名冲突。只支持当前 AER 的说明型 `SKILL.md`，脚本、附件及文件操作不会被静默接受。每轮最多生成一个完整库候选，可包含多项技能修改。
7. AER 复用既有配对验证与回归保护：基线读取旧技能库，候选读取新技能库，两者继续使用 Codex 执行；验证通过并被选择后，VersionStore 提交新的稳定 Snapshot。拒绝、无修改、模型异常或超预算均不更换稳定库。候选只更新 `skill` 工件，其他组件沿用父版本。

替换执行 Agent 时，实现既有技能读取能力及公开回执即可，不必修改 SkillClaw。替换进化组件时，实现 AER Evolver 协议并返回 `skill` 候选工件即可，执行侧不依赖 SkillClaw。当前插件注册支持 Codex 和按需读取的 ReAct；新增 Agent 还需声明相应插件兼容能力。

## 上游代码与范围

源码由 `third_party/SkillClaw` Git submodule 管理，固定为 `334ddf82856826b7a259470c6670fb0ef5ce2dc4`。适配器直接加载该版本 `evolve_server/pipeline/` 的原生 summarizer、aggregation、execution 及可选 verifier。使用私有 Python 命名空间跳过会连带导入其他 Agent 引擎的顶层初始化，不改写上游算法源码、不启动其存储轮询/HTTP 服务，也不使用其 OpenAI 客户端。

会话格式转换、AER 模型调用、候选库物化和稳定版本晋升由 AER 管理，未复刻完整 SkillClaw 服务部署。`verify_candidates` 默认关闭；启用后调用上游 LLM verifier 做额外筛选，但它不能替代 AER 的任务验证或自行晋升稳定版本。

## 预算与记录

### 三条行为边界

- 路由到 `skill` 只表示尝试这个方向。Agent 行为问题、证据不足或原技能已足够时，允许上游返回 `skip`，保存其理由和 `model_assessment` 来源，正常结束为 `empty`，不产生候选。缺少理由的 skip 会记录为无效输出，不替模型编造原因。
- `__no_skill__` 仅表示没有核验到读取，不证明能力缺失。记录中保留当时的技能名称、未遍历未读技能的事实，以及描述不合适、Agent 未选择、可能缺少技能或证据不足这些可能解释。保持原生 create/skip 分支，不自动增加全库描述优化；新建技能标记 `missing_capability_established: false`。
- 每个技能候选、每个验证任务保存 `skill_usage`：分别检查基线/候选的目录可用与发现状态、实际读取、预加载、执行身份、Snapshot、库工件和 `SKILL.md` SHA-256 摘要。按完整库的实际差异识别正文/描述变化，不依赖模型声明的 action。缓存命中核对原始执行的回执，不误用本次缓存复用的 operation ID。

发现只证明客户端发现服务或目录被提交给模型，不证明模型关注；读取只证明正文被提供，不证明采用。历史记录缺少字段时明确为未知。候选正文未读时，即使得分提高，也标为 `not_read_cannot_attribute`；核验读取只标为 `verified_exposure_only_not_causal`。描述变化结合基线/候选读取选择对比，不能直接解释成正文收益。

本次保持评分接受策略：未读取不自动否决得分，但限制解释。若出现明确的错误库、Snapshot、执行身份或正文摘要，则该配对验证证据无效，报告错误且不晋升。独立验证的细节仍只用于评测及研究报告，不额外送给当轮 Evolver。

这些字段保存在原始 `local_validation` / `validation_results` 工件及 `record.json` 的 `candidates[].validation.tasks[].skill_usage` 中。`evolution.txt` 同时展示 skip 理由、no-skill 边界与逐任务技能使用表。

插件分别限制调用次数、总 token、每次输出、单次模型超时、整轮时间及技能修改数量。模型超时由已有 provider 执行；适配器还在阶段间检查总时限。请求前使用 UTF-8 字节数做保守的输入预算估计，真实用量只取 provider 报告。未知 token 用量、被截断的模型输出或格式不合格会停止本轮。上游内部捕获的模型异常也会传回 AER，不会被误记为正常 skip。默认不重试模型请求。

公开事件、单字段及反馈分别有长度上限，裁剪会在会话 provenance 或文本中明确标注；上游还会进行自身的轨迹压缩。它们是公开可见事件，不是完整内部推理，也不把任务结果得分伪装成逐步 PRM。

每轮保存 `skill_evolution` 工件，包含源版本、证据、适配会话、原生阶段结果、模型请求/响应、用量、候选变化或失败原因。`rounds/.../record.json` 的 `generation.skill_evolution` 提供完整记录，`evolution.txt` 提供摘要；公开历史通过现有 ConstructionFeedback 的 `component=skill` 保留失败或 skip 原因。

## 验证

```powershell
.\.venv\Scripts\python.exe -m unittest tests.evolution.test_skillclaw tests.integration.test_codex_skillclaw -v
.\.venv\Scripts\python.exe -m unittest discover -s tests -t .
```

专项测试使用真实锁定的上游流水线、实际 MCP 读取、Codex 协议夹具和 HTTPX 模拟的 Qwen Chat Completions 响应，覆盖晋升/拒绝、版本隔离、原库保留、描述优化、新建/跳过、读取证据、异常和预算。测试不调用在线模型；真实 Codex/Qwen 的进化质量需要另行实验评估。
