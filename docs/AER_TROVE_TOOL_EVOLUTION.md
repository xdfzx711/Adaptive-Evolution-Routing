# TroVE 工具库进化插件

`trove_tool_evolver` 通过现有 `EvolutionPlugin.evolve` 接口返回一个 `tool` 工件，内容是完整的可复用 Python 工具库。Router 只返回 `target=tool`；内部的生成、复用、执行检查、筛选和裁剪由插件决定。

实现位于 `plugins/evolution/trove/`，算法参考 [TroVE 论文](https://arxiv.org/abs/2401.12869) 和[作者代码](https://github.com/zorazrw/trove)。这是适配 AER 的独立实现，没有引入上游实验脚本、模型加载器或数据集循环。原有 `tool_selection_evolver` 继续用于固定目录中的工具选择实验。

## 接入与配置

SWE-bench 运行配置为 `experiments/configs/v0_1_codex_swebench_trove.yaml`：主任务是 `psf__requests-1963`，Prompt Evolver 为 `gepa_prompt_evolver`，Tool Evolver 为 `trove_tool_evolver`。只允许进化 `prompt` 和 `tool`，最多五轮，沿用已有静态补丁评分和配对验证。执行模型为 `gpt-6.1-sol`，GEPA 反思模型沿用 `.env` 中的 Qwen 配置。

```powershell
.\.venv\Scripts\python.exe main.py run-task experiments/configs/v0_1_codex_swebench_trove.yaml --task-id psf__requests-1963 --output swebench-prompt-trove-result.json
```

新增示例 `experiments/configs/v0_1_codex_trove.yaml` 使用现有 Codex 执行 Agent 和 Qwen ModelProvider，开启跨任务积累。沿用项目的 `.env` 和 Codex 登录配置：

```powershell
.\.venv\Scripts\python.exe main.py validate-config experiments/configs/v0_1_codex_trove.yaml
.\.venv\Scripts\python.exe main.py run-task experiments/configs/v0_1_codex_trove.yaml --task-id hard-dev-001
```

上述命令由 Router 在 `prompt` 和 `tool` 两个组件中选择。单独验证 TroVE 链路时，可追加 `--set evolution.mode=fixed --set routing.plugin=fixed_router --set routing.fixed_target=tool --set evolution.stop_on_success=false`。这样初始任务成功后仍会尝试工具进化；是否接受候选继续由原有验证规则决定。

已有实验只需添加工具运行时配置，并绑定 Evolver：

```yaml
agent:
  config:
    tooling:
      catalog: {}  # 可同时保留现有 local/MCP 工具目录
      generated_python: true
    initial_tools: []
targets:
  allowed: [prompt, tool]
  default_operators:
    tool: trove_tool_evolver
  operator_configs:
    tool:
      samples_per_mode: 2
      selection_strategy: tool_checks
      max_library_tools: 32
      check_timeout_seconds: 5.0
      max_search_seconds: 120.0
```

上述片段应合并进已有配置，保留其他组件的绑定。`generated_python` 默认关闭。没有固定工具的空工具库也可以 CREATE。工具库跨任务传承遵循原有 `state.task_mode`：`independent` 从实验初始版本开始，`continual` 继承上一任务的稳定版本。

## 工具库工件

`Snapshot.artifacts.tool` 仍是标准 `ArtifactRef(kind="tool")`。新格式是自包含 JSON：

```json
{
  "schema_version": "aer.trove.toolbox.v1",
  "enabled_tools": ["path_info"],
  "tools": [{
    "name": "path_info",
    "description": "Describe a path string",
    "input_schema": {
      "type": "object",
      "properties": {"path": {"type": "string"}},
      "required": ["path"],
      "additionalProperties": false
    },
    "source": "def path_info(path):\n return {'is_python': path.endswith('.py')}",
    "dependencies": [],
    "examples": [{"arguments": {"path": "example.py"}, "expected": {"is_python": true}}]
  }],
  "usage_counts": {}
}
```

每个工具接收与 input_schema 对应的关键字参数，返回 JSON 对象。源码只定义一个同名函数，辅助函数可嵌套；依赖其他工具时显式声明 `dependencies`。实现随工具工件保存，因此正常的版本存储、配对执行、缓存区分和恢复均覆盖工具实现，无须修改 AER 核心。

`enabled_tools` 决定向 Agent 暴露的函数，库内未暴露函数仍可作为依赖。原有 `{"enabled_tools": [...]}` 工件继续按原格式读取。生成工具不能覆盖固定目录的工具名。

## 内部搜索

每次请求独立读取稳定版本的工具库，进行以下搜索：

1. IMPORT：试用当前生成工具；空库时省略此分支。
2. CREATE：提出新函数或替换已有函数，每个函数提供行为样例。
3. SKIP：使用基础 Python 能力求解，作为保留现状的选项。
4. 在独立 Python 子进程中检查候选库的全部行为样例，包括被替换函数的依赖方。生成器提供的样例用于筛除行为不符的候选，不代表独立正确性证明。
5. 根据配置在内部选择一个候选；超过容量上限时移除低频且不被其他工具依赖的函数，保留本次新增及实际使用的工具。
6. 只有工具库发生实际变化才返回 AER 候选；IMPORT/SKIP 胜出或选择器返回 null 时可返回 `empty`。SKIP 不删除稳定工具库。

| 策略 | 内部选择依据 | 用途 |
| --- | --- | --- |
| `tool_checks`（默认） | ModelProvider 结合公开任务、轨迹和工具行为检查选择候选 | 包括代码辅助工具在内的通用适配 |
| `execution_agreement` | 成功执行的解答输出一致性，再按解答的 AST 操作数量排序 | 数学、结构化问答等最终输出可比较的任务 |

`tool_checks` 允许不生成完整求解程序。`execution_agreement` 要求各样本提供 `def solve(): ...`，以公开上下文 `task` 为输入，返回可比较的 JSON 对象。这里没有进行补丁文本投票，也不将多个补丁相同视为修复正确。

该实现的通用任务选择器和按容量裁剪属于 AER 适配；原 TroVE 使用执行一致性选择，并按任务流进度周期性裁剪。当前实现不声称严格复现原论文实验。只选定一个工具库交给 AER；内部采样数与外层 `max_candidates_per_round` 分开配置。

## 执行与验证

Codex 工具适配器识别工具库工件，通过现有 MCP 桥接暴露生成函数，可与原有 local/MCP 工具共同使用。Agent 自主决定是否调用。工具以 JSON 数据处理为范围，可用于数学、文本和源码片段分析；源码由 Agent 读取后作为参数传入，生成函数不直接获得任务工作区或连接凭据。

Python 子进程使用独立临时目录、有限环境变量、受限 builtins 和标准库导出列表，限制私有属性与动态执行，并设置调用超时。子进程和源码限制不是操作系统沙箱；当前实现适用于受控实验，不能作为运行敌对 Python 代码的安全隔离层。

内部搜索只使用 EvolutionContext 的公开证据与生成样例，不读取验证任务、参考补丁或评分私有数据。AER 仍按原有配对验证和接受规则判断任务收益并提交；工具候选被拒绝、执行失败或验证中断时，稳定工具库保持原版本。工具样例通过不等于最终任务正确。当前静态 SWE-bench 评分的解释范围仍由现有 Evaluator 决定。

内部模型调用及 Python 执行计入 `EvolutionResponse.usage`，完整搜索记录通过 `record_ref` 指向 `trove_search` 工件，包含各分支、检查、选择、错误和实际模型响应。工具调用沿用 MCP 的装配、发现和调用记录。

## 验证

离线测试使用录制模型响应和真实 Python/MCP 子进程，覆盖空库生成、依赖检查、工具执行、内部筛选、超时、费用保留，以及现有 AER 路由、配对验证、提交与拒绝后的版本保持：

```powershell
.\.venv\Scripts\python.exe -m unittest tests.tools.test_trove_library tests.tools.test_trove_bridge tests.evolution.test_trove tests.integration.test_trove_startup -v
```

这些测试验证插件集成行为，真实任务收益需要另行运行实验。
