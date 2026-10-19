# AER 显式插件注册与依赖装配

开发步骤 4 已实现。配置可构建 Agent/Router/Evolver 的不同 Mock 组合；步骤 5 加入 [本地存储初始化](AER_LOCAL_STORAGE.md)，步骤 7 加入 [本地 Benchmark 与评分](AER_LOCAL_BENCHMARK.md)，步骤 8 加入 [实际执行与诊断](AER_AGENT_EXECUTION.md)，步骤 9 加入 [路由与候选生成](AER_PROMPT_EVOLUTION.md)，步骤 10 加入 [配对验证与接受选择](AER_PAIRED_VALIDATION.md)。主控制循环仍按后续步骤接入。

## 1. 分层与入口

| 位置 | 职责 |
| --- | --- |
| `aer_core/schemas/plugins.py` | 插件标识、能力、实例绑定计划与装配 manifest |
| `aer_core/plugin_registry.py` | 显式注册、重复检查、版本/工厂签名检查、生成配置声明 |
| `aer_core/plugin_manager.py` | 配置预检、依赖图检查、按依赖顺序实例化、接口与能力检查 |
| `plugins/mocks.py` | 两个 Mock Agent、两个 Mock Router、两个 Mock Prompt Evolver，以及进程内工件 fixture |
| `experiments/bootstrap.py` | 导入插件、注册工厂、将 YAML 转为绑定计划、提供依赖 |
| `aer_core/contracts/bundle.py` | ControllerPlugins，只包含供控制器使用的接口实例 |

核心 Registry/Manager 不导入具体插件。启动层导入 `plugins.mocks` 并注册；未来控制器接收 ControllerPlugins，无需了解具体类、工厂或 import 路径。ControllerPlugins 当前只覆盖本步骤的三个组件，后续评估和存储接口随实现加入。

命令行装配入口：

```powershell
.\.venv\Scripts\aer.exe assemble-config experiments/configs/mock_fixed_a.yaml
.\.venv\Scripts\aer.exe assemble-config experiments/configs/mock_fixed_b.yaml
.\.venv\Scripts\aer.exe assemble-config experiments/configs/mock_manual.yaml
```

支持 `--benchmark-index` 和 `--output`，路径规则与 validate-config 相同。成功输出 UTF-8 JSON，失败退出码为 2，stderr 给出错误代码与插件/依赖位置。

## 2. 声明与可实例化插件

原始三份 v0.1 YAML 均已可装配真实组件。Fixed/Manual Router 和 Prompt Evolver 通过显式工厂构建；execute-task 只构建执行与评估依赖，propose-prompt 另外构建路由与生成依赖。

新增 `llm_router` 与 `step_limit_evolver`，仍通过相同 Registry/Manager 装配。Router 声明 ModelProvider 与 ArtifactStore 依赖；步数 Evolver 只依赖 ArtifactStore。`targets.operator_configs` 透传并校验各 Operator 参数。Agent 可注册 `initial_artifacts(config) -> dict[str, bytes]` 回调，供启动层创建初始工件；未注册时兼容旧 Prompt 初始化。`snapshot_config` 继续描述冻结配置，实际可进化的步数保存在独立工件中。详见 [Qwen 路由与执行步数进化](AER_LLM_ROUTING.md)。

Mock 示例使用明确的 mock_* 名称。默认 Registry 注册六个 Mock、三个本地存储、三个 Benchmark/评分（toy_tasks、hard_local、确定性评分器）、六个执行层工厂、三个路由/进化工厂和两个验证/接受工厂，共 23 个。validate-proposal 通过 experiments.validation 装配 Harness、接受策略和评估侧依赖。配置 catalog 由声明与注册元数据合并生成。validate-config 不创建运行插件实例。Qwen 凭据由启动层读取 .env 并作为 runtime-only 依赖注入，见 [Qwen 接入](AER_QWEN_PROVIDER.md)。

配置校验依据 Router 声明的 routing_modes 判断 fixed/manual 兼容性；已有 fixed_router/manual_router 分别声明其原始模式。Agent 的固定步骤约束也来自声明，single_call_agent 继续要求 max_steps=1。

## 3. 插件标识与版本

PluginMetadata 包含 id、category、api_version、plugin_version、capabilities、dependencies。PluginDescriptor 补充工厂限定名和 Pydantic 生成的 config_schema。

注册键是 `(category, id)`，同一 Registry 中该键只能有一个工厂和插件版本。当前 API 为 `1.0`，要求插件 API 与 Registry 精确相等；api_version 使用 major.minor，plugin_version 使用 major.minor.patch。版本不兼容或格式错误在注册时拒绝，Manager 在装配时再次核对 API。

每次 build 独立构建实例，不进行运行中热切换。同一构建内共享依赖只创建一次；不同构建不共享实例缓存。装配 manifest 固定记录实际 API/插件版本、工厂、配置 schema、解析后插件配置和依赖绑定名，未来 Runner 将它写入运行 manifest。

运行入口根据已选 Benchmark、Evaluator、Agent、Diagnoser、Router 和 Evolver 的依赖声明补齐共享 Provider；即使 Agent 不使用模型，评分器、诊断器或 Evolver 所需的模型仍会构建。Benchmark/评分装配阶段创建的 Provider 会被后续执行装配复用。诊断器绑定不再按 llm_diagnoser 名称分支，接口方法、必要属性和依赖类型在装配时检查。完整 descriptor 用于内存装配检查，落盘的 run_manifest.json 使用精简的插件与版本清单。

Agent 注册可提供同步 `snapshot_config(experiment_config)` 回调，声明它自己控制的 Snapshot.config。省略时使用最小 max_steps 配置；ReAct 与 SingleCall 的注册回调提供各自需要的模型和工具配置。存储初始化、冻结版本导入和 assemble-config 都使用同一回调，不在通用代码里判断某个 Agent 名称或强制 ReAct 配置。新增 Agent 时只需实现、注册及配置，无须修改主控制器。

## 4. 工厂与依赖图

工厂使用统一同步签名：

```python
def factory(*, config, dependencies):
    return MyPlugin(config, dependencies.get("required_service"))
```

config 是登记的 Pydantic 类型；dependencies 是只读 ResolvedDependencies，包含已检查的实时对象，不进入 JSON。登记时同时提供依赖名称及其运行时类型或 runtime_checkable Protocol。

PluginBinding 给实例命名，引用 category/plugin_id，并把每个依赖名映射到另一个 binding 或启动层提供的外部对象。一个声明依赖必须恰好有一个来源；缺少、多余、名称冲突、循环和外部类型不兼容都会报 DependencyError。

Manager 在调用工厂前检查全部配置、外部依赖及循环；配置采用 strict=True、extra=forbid 校验。随后按依赖拓扑顺序创建实例，检查绑定依赖类型及 Protocol 签名，并注入工厂。

生成实例必须提供所属分类的同步接口方法，且支持接口的参数调用形式。Manager 另外检查 Agent/Evolver 的 capabilities() 返回类型和注册能力一致性；实际 run/select/evolve 的返回值与业务行为通过契约/集成测试验证。

一个简单的独立注册示例：

```python
from aer_core.plugin_registry import PluginRegistry
from aer_core.schemas.plugins import PluginBinding, PluginMetadata
from aer_core.plugin_manager import PluginManager

registry = PluginRegistry()
registry.register(
    PluginMetadata(
        id="my_router", category="router",
        api_version="1.0", plugin_version="0.1.0",
    ),
    router_factory,
    config_model=RouterConfig,
)
built = PluginManager(registry).build([
    PluginBinding(name="router", category="router", plugin_id="my_router", config={}),
])
router = built.instances["router"]
```

示例中的 RouterConfig/router_factory 由调用者实现。接入完整实验配置时，Router 还需声明支持的 routing_modes，并由启动装配层提供相应配置。

## 5. Mock 组合与依赖注入

| 示例 | Agent | Router | Evolver | mode |
| --- | --- | --- | --- | --- |
| mock_fixed_a.yaml | mock_agent_a | mock_router_a | mock_prompt_evolver_a | fixed |
| mock_fixed_b.yaml | mock_agent_b | mock_router_b | mock_prompt_evolver_b | fixed |
| mock_manual.yaml | mock_agent_b | mock_router_b | mock_prompt_evolver_a | manual |

两个 Agent 都返回固定的不同答案和标准 ExecutionResult；两个 Evolver 在旧 Prompt 后附加不同文本，并产生真正可读的进程内工件引用。它们不调用模型，成本明确为已知的零模型/工具消耗，不构成算法收益实验。

MockRouterA 支持 fixed；MockRouterB 支持 fixed/manual，手动模式从当前任务历史判断下一轮，并按配置决策表选择。该 fixture 假定上下文已按当前任务 episode 组织历史；正式 Manual Router 的完整 reset/stop 集成在后续步骤实现。

启动层给 Mock Agent 注入 initial_snapshot，给 Evolver 注入共享 artifact_store。默认初始 Prompt 来自 YAML，Snapshot 属于选中的 Agent，工件在进程内保存，不创建 runs 目录。禁用进化时只创建 Agent。

Python 入口可使用调用者的 Registry 和依赖对象：

```python
from experiments.bootstrap import assemble_experiment

assembled = assemble_experiment("experiments/configs/mock_fixed_a.yaml")
controller_plugins = assembled.plugins
manifest = assembled.built_plugins.manifest
```

assemble-config 加载冻结 Snapshot 或接入自定义 Agent 时需要显式提供 initial_snapshot；调用者提供的 Snapshot 必须与选中 Agent 匹配。步骤 5 的 init-storage 已实现冻结 Snapshot 的持久化导入。提供自定义依赖的入口是 assemble_experiment(..., dependencies={...})；通用图装配使用 PluginManager.build。

## 6. 错误与验收

装配异常均为 setup 阶段的 PluginError，继承步骤 3 的结构化 ErrorInfo：duplicate_plugin、plugin_not_found、plugin_version_error、plugin_registration_error、plugin_configuration_error、dependency_error、plugin_instantiation_error；不合格接口使用 contract_error。

测试覆盖六个工厂登记、八种配置组合切换、禁用进化、共享依赖只创建一次、manifest JSON 往返、重复注册、API 不兼容、缺少依赖、依赖循环、配置字段错误、错误方法签名和异步方法。CLI 测试覆盖三份 Mock 配置和包含中文的手动决策输出。

assemble-config 构建 Agent 及其依赖，并按 mode 构建 Router/Evolver，不执行任务。Benchmark 与持久存储分别由 inspect-benchmark、init-storage 构建；execute-task 合并执行、评分、诊断与存储。完整 Runner 后续实现。
