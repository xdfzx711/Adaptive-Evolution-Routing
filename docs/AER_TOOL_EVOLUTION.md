# 工具选择进化

生成并维护工具库的 TroVE 插件见 [TroVE 工具库进化](AER_TROVE_TOOL_EVOLUTION.md)。本文描述固定目录工具选择实验。

`tool_selection_evolver` 是独立 Evolver 插件。Router 选择 `tool` 后，由现有框架 ModelProvider（示例仍为 Qwen）提出下一次任务执行所用的完整附加工具集合。Codex 自己决定何时调用工具；Prompt、执行模型、推理强度、基础 Shell/Git/rg 能力保持原样。

## 启动

安装新增依赖：

```powershell
.\.venv\Scripts\python.exe -m pip install -e .
```

新增示例在原 Jev 三题配置上启用工具进化，仍是三份原始主任务、各自的同类与回归问题、最多五轮。原有配置没有改动。沿用 `.env` 中的 Jev/Qwen 配置和本机 Codex 认证：

```powershell
.\.venv\Scripts\python.exe main.py validate-config experiments/configs/v0_1_codex_swebench_jev_tools.yaml
.\.venv\Scripts\python.exe main.py run-task experiments/configs/v0_1_codex_swebench_jev_tools.yaml --task-id psf__requests-1963 --output requests-tools-result.json
.\.venv\Scripts\python.exe main.py run-task experiments/configs/v0_1_codex_swebench_jev_tools.yaml --task-id pytest-dev__pytest-7373 --output pytest-tools-result.json
.\.venv\Scripts\python.exe main.py run-task experiments/configs/v0_1_codex_swebench_jev_tools.yaml --task-id pylint-dev__pylint-6506 --output pylint-tools-result.json
```

上述命令由 Jev 在 prompt/model/tool 中选择，不保证每次一定路由到 tool。只验证工具进化时，可在 run-task 命令追加：

```powershell
--set evolution.mode=fixed --set routing.plugin=fixed_router --set routing.fixed_target=tool --set 'routing.config={}'
```

模型／推理强度的联合进化仍按 [Codex 配置](AER_CODEX_AGENT.md) 显式开启；此示例没有覆盖其旧默认值。

## 配置与版本

在 Codex 的 `agent.config` 中添加：

```yaml
tooling:
  catalog:
    python_structure:
      kind: local
      plugin: python_structure
    python_syntax_check:
      kind: local
      plugin: python_syntax_check
  startup_timeout_seconds: 30.0
  tool_timeout_seconds: 30.0
initial_tools: []
```

同时在 targets.allowed 中加入 tool，在 targets.default_operators 中配置 `tool: tool_selection_evolver`。也可以通过现有 --set 覆盖这些字段。

| 字段 | 含义 |
| --- | --- |
| tooling | 可选；省略时维持原有执行方式，不创建 tool 工件 |
| tooling.catalog | 本实验允许选择的工具 ID 到插件／服务实现的映射 |
| initial_tools | 创建初始工具工件时使用的 ID 列表；启用 tooling 后默认空集合 |
| tooling.servers | MCP 上游连接，保持在私有执行配置中，不披露给 Router/Evolver |
| tooling.dotenv_path | 可选工具凭据文件；相对路径以 AER 项目根目录为基准，例如 `.env` |
| tooling.startup_timeout_seconds | 一个上游服务的连接、初始化及工具发现上限 |
| tooling.tool_timeout_seconds | 一次附加工具调用的上限；Codex 完整执行仍受 agent.config.timeout_seconds 限制 |

工具 ID 不重复，使用以字母开头、最长 64 字符的字母／数字／下划线／连字符。Snapshot.config 冻结工具目录、实现与连接配置，公开反馈仅披露名称、描述、版本及可用参数规格。认证值在桥接运行时读取环境变量或 dotenv，不写入候选、经验或日志。

Snapshot.artifacts.tool 保存规范排序的选择，例如：

```json
{
  "enabled_tools": [
    "python_structure",
    "python_syntax_check"
  ]
}
```

Evolver 返回 enabled_tools 的完整列表：新增、移除或替换工具均可；`[]` 移除所有附加工具，`null` 表示没有提案。集合相同但顺序不同也视为空候选。未知 ID、重复 ID 和注入额外执行字段会被拒绝。工具实现、命令与地址由实验配置确定，不由 Evolver 生成。

旧配置和旧 Snapshot 继续使用 prompt/model；只有显式启用 tooling 的新实验才要求 tool 工件。不要将旧 Snapshot 直接套用到工具目录不同的新配置，候选验证期间也不能修改该目录。

## 首版本地插件

| 工具 | 输入 | 输出 |
| --- | --- | --- |
| python_structure | 一个相对于本次工作区的 `.py` 文件路径 | 类、函数、方法、导入和源码行号；大结果明确截断 |
| python_syntax_check | 同上 | 语法是否有效；错误行、列与原因 |

两者读取单个最大 1 MiB 的文件，检查解析后的实际路径，禁止绝对路径、越界和指向工作区外部的符号链接；不导入项目代码、不安装项目依赖、不运行项目测试、不写入仓库。语法无效属于检查结果，并不等同于工具基础设施执行失败。

本地工具通过独立 LocalToolRegistry 显式注册；新增实现时注册 LocalTool（插件 ID、版本、公开规格和调用函数），再加入实验目录。MCP 桥接负责暴露所选 ID，不固定工具名称。当前基础命令执行仍可自行完成类似分析，工具的实际收益由评分与配对验证决定。

## 已有 MCP 服务

STDIO 例子（替换为已安装服务的实际命令、公开描述与其 initialize 返回的版本）：

```yaml
tooling:
  catalog:
    project_lookup:
      kind: mcp
      server: project_index
      tool_name: lookup
      description: Look up symbols from the current task repository index.
      version: 1.0.0
  servers:
    project_index:
      transport: stdio
      version: 1.0.0
      command: [python, 'D:/tools/project_index.py', --root, '{workspace}']
      env_vars: [PROJECT_INDEX_KEY]
  dotenv_path: .env
initial_tools: []
```

命令列表的 `{workspace}` 替换为本次实际工作区；上游 STDIO 进程的 cwd 也设为此目录，MCP roots 只声明这个工作区。仅连接当前选中的工具所需的服务，仅暴露选中的工具别名。

HTTP 连接可替换 servers 中的条目为：

```yaml
project_index:
  transport: streamable_http
  version: 1.0.0
  url: https://your-service.example/mcp
  bearer_token_env: PROJECT_INDEX_TOKEN
  header_envs:
    X-Project: PROJECT_INDEX_PROJECT
```

HTTP 地址不接受内嵌凭据或查询参数；认证通过环境变量引用配置。初版支持 Bearer 和环境变量请求头，不自动进行 OAuth 登录。上游版本、所选工具名称以及可选 input_schema 与实际发现结果不匹配时启动失败，不静默换服务或换工具。第三方服务须按任务配置限定自身的数据范围；cwd 和 MCP roots 不是第三方进程的文件系统沙箱，不能据此假定其访问权限受到强制限制。

## 执行、验证与日志

每次旧版／候选执行创建独立 MCP 桥接会话，按所传 Snapshot 装配工具。服务启动、实际工具发现和调用由该会话记录。未选中的继承 MCP 服务继续关闭，不修改全局 Codex 配置。临时配置和状态文件放在任务工作区外，结果合并进现有执行记录后清理，不形成额外的持久日志文件。

工具声明已选择但启动失败、发现不完整或桥接没有生效时，本次执行报错；验证沿用现有的不完整／异常分支，不提交稳定版本。成功发现但没有调用工具时如实记录，仍按任务输出评分。拒绝与验证中断保留原稳定工具集合，接受提交后继承新的集合。Evolver 没有提交权限。

工具工件、冻结目录和连接配置进入 Snapshot，适配器版本进入评测条件。工具集合、版本或配置不同，不会复用同一个验证缓存。对于依赖外部动态数据的 MCP 服务，应固定服务及数据版本；只固定服务器版本号不能保证数据内容不变。

evolution.txt 简要显示附加工具、装配状态、实际可用工具和调用结果；每轮 record.json 保留完整的候选、配对执行状态和工具轨迹。不会强制 Agent 使用某个工具，也不会因为增加工具本身给予奖励。

通用选择类型与 ExecutionToolAdapter 位于 plugins/tool_selection.py。CodexToolAdapter 位于 plugins/agents/tool_selection.py，ToolSelectionEvolver 位于 plugins/evolution/tool_selection.py。未来 Agent 可实现自己的工具适配器并注册兼容关系，复用同一个 Evolver；主控制器、Benchmark、Evaluator 和接受策略无须识别工具实现。

协议依据：[Codex MCP 配置](https://learn.chatgpt.com/docs/extend/mcp)、[MCP Python SDK v1 客户端](https://py.sdk.modelcontextprotocol.io/v1/client/)。SDK 固定在 1.x 维护线，避免安装时跨越到不兼容的 2.x 接口。
