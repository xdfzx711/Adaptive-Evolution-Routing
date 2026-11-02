# 固定技能执行

技能内容由 AER 的 Snapshot 管理。固定技能执行支持导入、发现、读取和版本隔离。默认实验不自动进化技能；需要进化时显式选择 [SkillClaw 固定目标或自适应路由配置](AER_SKILLCLAW_EVOLUTION.md)。

## 配置

实验入口默认启用项目自带的 experiments/skills 技能库，当前包含 task-output-contract。ReAct 与 Codex 默认按需读取；SingleCall 默认预加载 task-output-contract。默认路径相对于安装的 experiments 包定位，与实验 YAML 所在目录和当前工作目录无关。

无需修改现有 YAML 即可使用默认库。要关闭，在 agent.config 下设置 skills: null，或使用命令行 --set agent.config.skills=null。恢复之前未启用技能的旧 Snapshot 时，也应显式关闭，以保持其冻结配置一致。

ReAct 与 Codex 可在 agent.config 下指定自定义库：

```yaml
skills:
  initial_library: ../skills
  mode: on_demand
```

initial_library 相对于实验 YAML 解析。目录下每个子目录包含一个 SKILL.md，YAML frontmatter 必须包含唯一的 name 和非空 description，正文非空。v1 支持说明型技能，每个技能目录仅允许 SKILL.md；附件或脚本会明确报错。最多 64 个技能，名称最多 64 字符、描述 1024 字符、全文 65536 字符。

初始化时读取原文，按技能名称确定性排序并存为一个自包含的 skill JSON 工件。原文件换行保留；内容摘要因此也反映换行变化。源路径只保存在实验配置记录，不能决定技能内容身份。执行从 Snapshot 取工件，不再读取源目录；initial_snapshot_ref 恢复已有快照时不要求源目录存在。

省略 skills 使用上述默认库；显式配置不会与默认库合并。ReAct/Codex 的 skills: {} 仍表示启用空技能库，同样生成 skill 工件。运行中修改源目录不改变已冻结的版本。直接构造底层 Agent 的调用方仍需显式传入配置及匹配的 Snapshot；默认值由实验入口统一补齐。

单次调用 Agent 自定义技能库时，必须指定预加载模式和技能名称：

```yaml
skills:
  initial_library: ../skills
  mode: preload
  preload: [task-output-contract]
```

ReAct 也可选择 preload。Codex 当前仅实现 on_demand。不同加载策略在实验中应分别报告。

## 与 Agent 无关的接口

SkillLibrary 保存完整 SKILL.md 及其元数据。SkillRuntime 在每次执行时绑定 Snapshot 版本、技能工件和 execution_operation_id，提供 list_skills()、read_skill(name, call_id=...)。

AgentCapabilities.skill_delivery 声明 transport、discovery 和 observes_reads。ReAct 使用原生 read_skill 工具；Codex 使用独立的 aer_skills STDIO MCP 服务；SingleCall 使用显式预加载。其他 Agent 可实现相同接口的适配，不必依赖 MCP。

on_demand 只先展示名称与描述索引，读取时才返回正文。read_skill 属于固定的读取基础设施，不依赖 tool 工件，也不参与工具选择进化。工具提供者若占用同名 read_skill，会报冲突。

## 隔离与记录

Codex 的技能会话资源放在任务仓库外的临时目录，服务只收到本次冻结的技能库。GitPatchCollector 不会采集这些资源。结束或异常时先提取读取记录，再释放服务目录。已有工具 MCP 可同时装配。

启用 AER 技能时，Codex 使用 skip_host_skill_discovery、关闭插件加载，并显式禁用任务根下 .agents/skills 与 .codex/skills 的原生技能自动加载。skip_host_skill_discovery 单独使用仍会发现仓库技能。相关行为通过本机 Codex CLI 0.162.1 与本地 Responses 模拟服务器验证；升级 CLI 时应重新运行该集成测试。仓库源码文件仍可作为任务输入读取，自动发现隔离并不删除源码中的技能文件。

ExecutionResult.state_summary 保存 available_skills、read_skills、injected_skills、skill_reads、skill_delivery、skill_setup_status 和 skill_library_artifact_id。读取凭据包含执行身份、Snapshot 版本、技能内容摘要和调用身份。MCP 的调用身份是连接内 JSON-RPC request ID。成功读取只表示正文已提供，不等于模型采用了指导。preload 只计入 injected_skills，不冒充主动读取；服务不可用与技能未被选用分别记录。

缓存键已有整个 Snapshot；运行条件包含技能运行时版本。两个技能库版本不会共用同一执行缓存。公共反馈仅暴露 Agent 明确声明的技能记录字段；加载器不读取评分规格或验证集私有反馈。

## 示例与验证

仓库内 experiments/skills 提供一个仅说明输出契约的示例，不包含 benchmark 答案。

```powershell
.\.venv\Scripts\python.exe main.py execute-task experiments/configs/v0_1_no_evolution.yaml --task-id dev-001
```

默认 scripted_model 不会自主选择 read_skill，此命令验证固定库装配；模型按需读取由确定性测试和真实 CLI 传输测试验证。配置 Qwen 等工具调用模型后可观察自主选择。无需安装或启动 SkillClaw 服务。

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests/skills -v
$env:AER_TEST_REAL_CODEX = '1'
.\.venv\Scripts\python.exe -m unittest tests.integration.test_codex_native -v
```

真实 CLI 测试使用本地模拟模型端点，检查正文进入下一次请求、A/B/A 版本读取、仓库技能自动发现隔离，不调用付费模型。其他测试覆盖源目录变化与删除后的快照恢复、并发读取、MCP 与直接调用一致性、预加载语义、服务异常和源码补丁的文件集合。
