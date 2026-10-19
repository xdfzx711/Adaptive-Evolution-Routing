# Adaptive Evolution Routing（AER）MVP 开发指导方案

> 版本：v0.1 设计基线 · 2026-10-12  
> 目标：构建插件驱动、可验证、可回滚、可接入不同 Agent / Evolution Method / Benchmark / Baseline 的自进化实验框架。

> v0.1 落地约定见 [范围与实验语义](docs/AER_V0_1_SPEC.md)，附三种运行模式的完整配置。本文中的接口、YAML 和控制循环仍是架构草案；实现时以该约定的字段、配置轮数、反馈权限和候选接受规则为准。

## 0. 设计结论与边界

### 0.1 核心原则

**核心框架只调用 Interface；具体功能由 Plugin 实现。**

- **Contract / Interface**：规定插件必须提供的输入、输出、行为、不变量和错误语义；不负责发现插件。
- **Plugin Registry / Manager**：负责插件发现、注册、版本检查、配置校验、依赖注入、实例化和替换；不定义业务行为。
- **Adapter**：将第三方 Agent / Evolution Method / Baseline 的原生 API 转换为 AER Contract；避免为了适配而篡改原方法核心算法。
- **Orchestrator**：只依赖抽象接口，负责调度和状态转换，不写具体 Agent、Router、进化策略或 Benchmark 的实现逻辑。
- **Artifact & Version Store**：候选修改先产生不可变版本，再在隔离验证后提交；不直接覆盖稳定版本。
- **Experience Store**：保留所有成功与失败的进化尝试；与“只接受更优版本”是两个独立规则。

### 0.2 第一阶段范围

| 能力 | MVP v0.1 必须 | 预留但不急于实现 |
|---|---|---|
| Agent | 自研 ReAct Agent + 第二个最小 Agent 用于可替换性测试 | Code Agent、第三方 Agent、多 Agent |
| Router | Fixed、Manual；Rule-based 可作为 v0.2 | Learned / RL Router |
| 进化 | Prompt Evolver；Memory Evolver 可作为 v0.2 | Tool / Code / Skill / Workflow / Model 等 |
| 评估 | 确定性 Task Evaluator + 轻量 Diagnoser | 复杂测试沙盒、多指标评估 |
| 验证 | 同一验证任务集上比较旧版与候选，接受或回滚 | 置信区间、复杂多目标 Pareto 比较 |
| 存储 | JSONL 经验库 + 本地不可变版本文件 | 数据库、分布式对象存储 |
| 适配 | 简易 Benchmark Adapter、Runner 接口 | SWE-bench 全量环境、完整外部 Baseline |

## 1. 总体架构与数据流

![mermaid-diagram (1)](C:\Users\Administrator\Downloads\mermaid-diagram (1).png)

**实际控制流程**：task → 基线 Agent 执行 → 外部评分/诊断 → 生成进化上下文 → 计算当前可行 Target 集合 → Router 选 Target → Resolver 选择该 Target 对应 Operator → 生成候选 Artifact / Agent Bundle → 在一致验证集上评估 → 接受或回滚 → 无论结果如何均保存经验 → 若预算允许，基于最新上下文再决策。

> 图示是逻辑关系而非调用栈：由 Orchestrator 驱动各步骤；失败后应重新构建上下文并携带上轮失败的决策反馈。不得把失败候选直接当作新的稳定版本。

## 2. 核心领域模型和数据契约

实现语言建议 Python 3.11+；`dataclasses` 或 Pydantic 均可。核心公共类型尽量独立、无外部 Agent SDK 依赖。推荐使用以下概念；字段可扩展，但跨插件语义必须保持一致。

### 2.1 Task 与执行结果

```python
from dataclasses import dataclass, field
from typing import Any

@dataclass(frozen=True)
class Task:
    task_id: str
    input: dict[str, Any]
    benchmark_id: str
    eval_spec_ref: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

@dataclass(frozen=True)
class TraceEvent:
    seq: int
    kind: str  # model_call | tool_call | observation | file_edit | error | finish ...
    payload: dict[str, Any]
    timestamp: str | None = None

@dataclass(frozen=True)
class ExecutionResult:
    task_id: str
    agent_version_id: str
    output: dict[str, Any]
    trajectory: list[TraceEvent]
    state_summary: dict[str, Any]
    usage: dict[str, float]  # tokens, time, tool_calls, etc.
    artifact_refs: list[str] = field(default_factory=list)

@dataclass(frozen=True)
class EvaluationResult:
    score: float
    success: bool
    metrics: dict[str, float]
    feedback: str
    evidence_refs: list[str] = field(default_factory=list)

@dataclass(frozen=True)
class Diagnosis:
    reason_code: str        # e.g. wrong_tool_use, missing_context, reasoning_error
    explanation: str
    evidence_step_ids: list[int] = field(default_factory=list)
    confidence: float | None = None
```

- `ExecutionResult` 为 Agent 的**事实性输出**；`EvaluationResult` 为外部评分，`Diagnosis` 为**可错的解释假设**。不要把 Agent 自报成功当作外部正确性。
- `TraceEvent.kind` 使用开放枚举或命名空间扩展。不同 Agent 只需统一事件信封 `seq/kind/payload`，不强迫相同内部推理轨迹；隐式模型推理文本不要求获取。
- 大型日志、补丁和仓库快照存到 Artifact Store，事件中保存引用，避免 Context 过大。
- 评分信息中的正确答案、隐藏测试和验证集标签**不能无条件暴露给 Evolver 或 Router**；上下文只提供实验协议允许的反馈，防止数据泄漏。

### 2.2 Artifact、AgentSnapshot、候选进化

```python
@dataclass(frozen=True)
class ArtifactRef:
    artifact_id: str
    kind: str           # prompt | memory | tool | code | skill | workflow | ...
    version: str
    uri: str
    digest: str         # e.g. sha256

@dataclass(frozen=True)
class AgentSnapshot:
    version_id: str
    agent_plugin_id: str
    config: dict[str, Any]
    artifacts: dict[str, ArtifactRef]
    parent_version_id: str | None = None

@dataclass(frozen=True)
class EvolutionContext:
    task: Task
    stable_snapshot: AgentSnapshot
    execution: ExecutionResult
    evaluation: EvaluationResult
    diagnosis: Diagnosis | None
    history_refs: list[str]
    feasible_targets: list[str]
    remaining_budget: dict[str, float]

@dataclass(frozen=True)
class EvolutionDecision:
    target: str
    rationale: str
    operator_id: str | None = None   # MVP resolved by policy, not learned

@dataclass(frozen=True)
class EvolutionCandidate:
    candidate_id: str
    parent_version_id: str
    target: str
    artifact_updates: dict[str, ArtifactRef]
    agent_bundle_ref: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

@dataclass(frozen=True)
class EvolutionOutcome:
    accepted: bool
    baseline_metrics: dict[str, float]
    candidate_metrics: dict[str, float]
    reward: float
    reason: str
    candidate_id: str
```

**不可变性约束**：Operator 不能原地修改 `stable_snapshot` 或 Agent 工作目录；提交新版本由 Version Manager 统一执行。若第三方方法有副作用，必须在隔离副本运行，并记录不可回滚能力。

### 2.3 经验记录

建议事件化存储，最小经验字段：

```text
ExperienceRecord = {
  experience_id, experiment_id, task_id, round_id,
  pre_state_ref, trajectory_ref, evaluation_before, diagnosis,
  feasible_targets, router_decision, operator_id,
  stable_version_id, candidate_version_id,
  evaluation_after, reward, accepted, failure_or_rejection_reason,
  budget_used, timestamp, provenance
}
```

经验库保存**全部尝试（包括拒绝、异常、无收益）**；Version Manager 只把通过验证的候选提升为新稳定版本，仍需保存候选工件用于审计（可按保留策略清理）。历史检索只暴露到当前时间点已知的经验，避免未来信息泄漏。

## 3. Interface 与插件管理的职责边界

### 3.1 Contract（协议）

每个 Contract 必须定义：输入数据类型、返回类型、调用前条件、错误语义、可选能力、幂等/状态隔离预期。核心框架只能 import `aer_core.contracts.*`、`schemas.*`，不得 import 某个插件实现。

建议的 Protocol（异步需求可后续统一扩展；MVP 先同步）：

```python
from typing import Protocol

class BenchmarkPlugin(Protocol):
    def tasks(self, split: str) -> list[Task]: ...
    def evaluation_spec(self, task: Task) -> dict: ...

class AgentPlugin(Protocol):
    def capabilities(self) -> dict: ...
    def run(self, task: Task, snapshot: AgentSnapshot, run_ctx: dict) -> ExecutionResult: ...
    def snapshot(self) -> AgentSnapshot: ...
    def restore(self, snapshot: AgentSnapshot) -> None: ...

class EvaluatorPlugin(Protocol):
    def evaluate(self, task: Task, result: ExecutionResult, eval_ctx: dict) -> EvaluationResult: ...

class DiagnoserPlugin(Protocol):
    def diagnose(self, task: Task, result: ExecutionResult, evaluation: EvaluationResult) -> Diagnosis: ...

class RouterPlugin(Protocol):
    def select(self, context: EvolutionContext) -> EvolutionDecision: ...

class EvolutionPlugin(Protocol):
    def capabilities(self) -> dict: ...
    def evolve(self, request: dict) -> list[EvolutionCandidate]: ...

class ValidationPlugin(Protocol):
    def validate(self, stable: AgentSnapshot, candidates: list[EvolutionCandidate], tasks: list[Task]) -> list[EvolutionOutcome]: ...

class ExperienceStore(Protocol):
    def append(self, record: dict) -> str: ...
    def query(self, criteria: dict) -> list[dict]: ...

class ArtifactStore(Protocol):
    def put(self, content: bytes, kind: str) -> ArtifactRef: ...
    def get(self, ref: ArtifactRef) -> bytes: ...

class ExperimentRunner(Protocol):
    def setup(self, config: dict) -> None: ...
    def run_task(self, task: Task) -> dict: ...
    def reset(self, scope: str) -> None: ...
    def close(self) -> None: ...
```

注意：上面是**接口草案**；最终实现需统一 `request` / `run_ctx` 等通用 `dict` 为专门 DTO，减少隐式约定，并定义 `PluginError`、`UnsupportedCapability`、`EvaluationError`、`BudgetExceeded` 等类型化异常。`snapshot/restore` 应针对**可控状态**；如果 Agent 不支持完整状态恢复，声明能力限制，不能默默破坏可回滚性。

### 3.2 Plugin Manager（发现、注册和替换）

插件标识至少包括：`id`、`category`、`api_version`、`plugin_version`、`factory`、`capabilities`、`config_schema`、`dependencies`。执行步骤：

1. **discover**：MVP 先显式注册，未来支持 Python entry points、安装包扫描或 remote adapter。
2. **validate**：检查 `api_version`、必要方法、配置 schema、依赖以及与当前 Agent/Target 的兼容性。
3. **instantiate**：Factory 创建实例并注入模型客户端、工具注册表、日志、存储等依赖。
4. **configure**：加载 YAML 配置，绑定预算与运行上下文；记录解析后的完整插件清单。
5. **replace**：同一实验使用确定性插件版本；实验之间改配置实现替换，运行中热切换须有明确快照/迁移约束。

最小注册器：

```python
class PluginRegistry:
    def __init__(self):
        self._factories = {}

    def register(self, category: str, name: str, factory):
        key = (category, name)
        if key in self._factories:
            raise ValueError(f"duplicate plugin: {key}")
        self._factories[key] = factory

    def build(self, category: str, name: str, **deps):
        return self._factories[(category, name)](**deps)
```

这是**演示版**，不等于完整 Plugin Manager；在生产化阶段必须增加 API 版本检查、兼容性、配置校验与依赖冲突管理。**Registry 不是 Interface**：注册存在不代表满足协议，满足协议也不代表自动被发现。

## 4. Evolution Target Space 与 Operator 解耦

### 4.1 初始九类 Target

| Target | 进化对象                           | 是否建议保留 | 理由                                        |
| ------ | ---------------------------------- | ------------ | ------------------------------------------- |
| Memory | 历史经验、长期记忆、记忆检索与组织 | 保留         | 代表 Agent 的经验积累与利用能力             |
| Prompt | System Prompt、任务指令、推理提示  | 保留         | 代表 Agent 的指令与行为引导方式             |
| Tool   | 工具定义、工具集合、工具调用配置   | 保留         | 代表 Agent 与外部能力的交互接口             |
| Code   | Agent 源代码、执行逻辑、控制流程   | 保留         | 代表 Agent 的程序结构及功能实现             |
| Skill  | 可复用的任务技能、操作流程及技能包 | 有条件保留   | 代表 Agent 的可复用任务能力，但需要单独定义 |

### 4.2 动态可行集合

$$
\mathcal{Z}_t = \mathcal{Z}_{\mathrm{agent}} \cap \mathcal{Z}_{\mathrm{plugins}} \cap \mathcal{Z}_{\mathrm{allowed}}
$$

- Agent 声明可修改组件、可导出哪些 Artifact、是否可回滚。
- Evolution Plugin 声明支持的 Target、Agent 类型、输入 Artifact、所需预算/权限、是否多候选及是否有内部搜索。
- Policy/Budget 对当前任务做过滤：例如只读任务禁止代码写入，预算不足禁用昂贵微调。
- `TargetRegistry.list_feasible(agent_capabilities, plugins, constraints)` 产生当轮候选集合；集合为空时应停止或标记无可用进化，不允许 Router 臆造目标。

**两层决策**：Router 先输出 `target`；`OperatorResolver` 按实验配置选该 Target 对应的具体 Operator。MVP 中一个 Target 指定一个默认 Operator。后续若研究需要，再扩展为 Operator 选择，不要提前混淆论文核心变量。

## 5. Agent Execution：自研起步、以后可更换

### 5.1 Agent Plugin 的职责

- 接收标准 `Task` + `AgentSnapshot` + `RunContext`。
- 执行 Agent 自身的推理、工具调用与任务完成逻辑；对外不暴露 ReAct 特有状态机。
- 产出 `ExecutionResult`，包括标准 TraceEvent 列表、输出、状态摘要、资源统计及 Artifact 引用。
- 声明 `capabilities()`：支持的可进化组件、模型/工具需求、是否支持 snapshot/restore、是否有内部状态、可用事件类型。
- 不评判自身最终是否通过 Benchmark；外部 Evaluator 是评分权威。

### 5.2 自研 ReAct Agent v0.1

```text
ReActAgent.run(task, snapshot)
  -> compose input from task + prompt artifact + memory artifact
  -> call ModelProvider
  -> if tool call: invoke ToolProvider and append tool_call / observation
  -> repeat until finish / max_steps / budget
  -> return ExecutionResult
```

建议内部依赖也通过轻量接口注入：`ModelProvider.generate()`、`ToolProvider.invoke()`、`MemoryProvider.read()`。第一版不要过度拆分 Planner、Scheduler、Reflection 等内部组件。`TraceEvent` 应记录工具输入/输出或其受控引用、错误、调用耗时与 token，避免把私密数据无过滤写进日志。

### 5.3 第三方 Agent Adapter

```python
class ExternalAgentAdapter:
    def __init__(self, external_agent, state_adapter, trace_normalizer):
        self.external_agent = external_agent
        self.state_adapter = state_adapter
        self.trace_normalizer = trace_normalizer

    def run(self, task, snapshot, run_ctx):
        native_result = self.external_agent.run(task.input)  # 示意：非通用 API
        return self.trace_normalizer(task, snapshot, native_result)

    def snapshot(self):
        return self.state_adapter.export()

    def restore(self, snapshot):
        self.state_adapter.load(snapshot)
```

具体第三方 API 由对应 Adapter 负责映射；对不具备 snapshot/restore 的 Agent，优先使用**独立进程/容器中的纯配置重建**，并在能力描述中标记限制。若无法保证隔离或重放，不应用于要求严格版本对照的实验。

## 6. Evaluation & Diagnosis：评分和归因分开

- `EvaluatorPlugin` 使用 Benchmark 指定的标准评分方法。TOY24 类任务可使用可验证答案规则；SWE-bench 类任务要使用隔离测试环境和对应任务的测试规范。
- `DiagnoserPlugin` 根据轨迹、评估反馈输出 `reason_code`、证据与置信度；仅是推测而非真实因果标签。
- 固定诊断标签建议从小集合开始：`wrong_tool_use`、`missing_context`、`incorrect_reasoning`、`execution_error`、`invalid_output`、`unknown`。诊断不直接输出进化目标，避免代替 Router。
- 按实验协议控制可见反馈：隐藏测试结果不能未经许可泄漏到 Evolver 的 Prompt 中；应区分训练/开发反馈与最终测试分数。

## 7. Router：先 Fixed/Rule，再 Adaptive

```python
class FixedRouter:
    def __init__(self, target: str):
        self.target = target

    def select(self, ctx: EvolutionContext) -> EvolutionDecision:
        if self.target not in ctx.feasible_targets:
            raise ValueError("configured target is infeasible")
        return EvolutionDecision(target=self.target, rationale="fixed strategy")
```

Rule Router 根据诊断标签及上下文选择目标，并在选择前检查 `feasible_targets`；不能用无约束字符串匹配当成可靠诊断。未来 `AdaptiveRouter` 可先读取结构化 Context，再加入 `G_t = Encoder(Context)`、经验检索和 RL 优化，不改变上层 `select()` 契约。

## 8. Evolution Plugins：自研与外部方法统一接入

### 8.1 通用行为

`EvolutionPlugin.evolve(request)` 只负责**生成候选**，不自行提交 Agent 稳定版本；返回候选 Artifact 集合或完整 Agent Bundle。允许一个调用生成多个候选，允许插件内部进行搜索，但须报告成本、输入依赖和内部评估使用情况。

MVP v0.1 实现 `PromptEvolver`：读取旧 Prompt、允许暴露的轨迹与诊断反馈，生成新 Prompt Artifact；MVP v0.2 实现 `MemoryEvolver`：从失败轨迹生成经过清洗的候选记忆，不能无审查写入长期共享记忆。

### 8.2 外部 Evolution Adapter

```text
AER EvolutionRequest
    ↓ convert_request
External method's native input
    ↓ external search / evolution
Native candidates + native metadata
    ↓ normalize_candidate
AER EvolutionCandidate[]
    ↓ AER Harness (independent final validation)
```

特别情况：外部方法可能自带 Agent、搜索空间、内层 evaluator、迭代进化，不应强拆为单一 Prompt 改写。接入时选择：

- **组件级适配**：提取可复用 Evolver，纳入 AER `EvolutionPlugin`，方便做组件消融。
- **方法级适配**：原样保留整套外部演化流程，作为 `ExternalBaselineRunner`，方便进行论文级方法对比。

当第三方 Evolver 修改多个 Target 或整套 scaffold 时，使用 `agent_bundle_ref` 或 `artifact_updates`，并记录主要 Target 和全部受影响对象；对无法回滚的副作用一律要求隔离执行。

## 9. Evolution Harness、三级验证与版本管理

| 验证模块                | 核心问题                                 | 评价指标                     |
| ----------------------- | ---------------------------------------- | ---------------------------- |
| Local Validation        | 本次进化是否解决当前失败任务？           | 当前任务成功率、分数变化     |
| Similar-task Validation | 本次进化能否改善独立的同类任务？         | 同类任务成功率变化、迁移收益 |
| Regression Validation   | 本次进化是否造成其他代表性任务明显退化？ | 性能下降幅度、回归失败数量   |

有一个统一的指标



## 10. Baseline、Benchmark 与实验 Runner

**Benchmark Adapter 与 Baseline Adapter 是不同层：前者规范任务和评分，后者规范参评方法的运行接口。**

### 10.1 两种接入方式

| 模式 | 用途 | 是否共用 AER Orchestrator | 对原方法的要求 |
|---|---|---|---|
| `ComponentAdapter` | 把外部 Agent / Evolver 作为可替换插件 | 是 | 输出 AER Contract |
| `ExternalBaselineRunner` | 评估完整论文方法 | 不必 | 尽量保留原本训练/进化逻辑，仅规范 I/O、资源和评价 |

统一 `ExperimentRunner` 生命周期：`setup(config)` → `run_task(task)` → `reset(scope)` → `close()`。`reset(scope)` 必须区分逐任务 reset、逐 episode reset、逐 benchmark reset，防止跨任务记忆泄漏或错误清空长期学习状态。

统一 `ExperimentResult` 至少保存：`task_id/method_id/seed/agent_version/output/eval_metrics/resource_usage/trace_ref/artifact_refs/provenance`。外部方法**缺少内部轨迹**时可声明 `trace_level=final_only`，不应编造与 AER 相同的深度信息。原论文若使用不同模型、工具或预算，必须在结果表明确标记，不能声称严格公平对照。

### 10.2 初始实验矩阵

| 方法 | 使用的 Runner | 特征 |
|---|---|---|
| 原始 Agent | AER Runner（禁用进化） | 基础性能 |
| 固定进化策略 | AER Runner + Fixed Router | 验证固定组件进化效果 |
| 规则路由 | AER Runner + Rule Router | 验证简单诊断选择 |
| AER MVP | AER Runner + MVP Router/Operator 组合 | 验证完整框架有效性 |
| 外部论文 Baseline | External Baseline Runner | 保留论文实现并统一评测 |
| Adaptive Router（Phase 3） | AER Runner + Adaptive Router | 验证目标选择能力 |

**避免基线重复计数**：当 MVP 恰好采用 Fixed Router 时，MVP 与“固定策略”可能是同一实验配置。实验表应该用 `agent × router × operator × benchmark × seed` 标识真实配置，不应将同一配置包装成两个独立方法。

Phase 2 要同时在 Benchmark 上运行 **Baselines 与我们的 MVP**，而不是只运行 Baseline。先用低成本可复现任务调试，再考虑 SWE-bench 类较重环境。

## 11. Orchestrator：唯一主控制循环

```python
class AEROrchestrator:
    def run(self, task, initial_snapshot, validation_tasks, max_rounds=3):
        stable = initial_snapshot
        history_refs = []
        for round_id in range(max_rounds):
            execution = self.agent.run(task, stable, self.run_ctx)
            evaluation = self.evaluator.evaluate(task, execution, self.eval_ctx)
            if evaluation.success:
                break
            diagnosis = self.diagnoser.diagnose(task, execution, evaluation)
            feasible = self.target_registry.list_feasible(stable, self.plugins, self.budget)
            if not feasible or self.budget.exhausted():
                break
            context = self.context_builder.build(
                task, stable, execution, evaluation, diagnosis,
                history_refs, feasible, self.budget.remaining()
            )
            decision = self.router.select(context)
            self.target_registry.assert_feasible(decision.target, feasible)
            operator = self.operator_resolver.resolve(decision.target)
            candidates = operator.evolve(self.request_factory.make(context, decision))
            outcomes = self.harness.validate(stable, candidates, validation_tasks)
            for candidate, outcome in zip(candidates, outcomes):
                experience_ref = self.experience_store.append(
                    self.make_record(context, decision, operator, candidate, outcome)
                )
                history_refs.append(experience_ref)
            chosen = self.harness.select_accepted(candidates, outcomes)
            if chosen is not None:
                stable = self.version_store.commit(stable, chosen)
            # rejected: stable unchanged; next round rebuilds context and includes failure history
        return stable, history_refs
```

这是**架构级伪代码**，`make_record`、`list_feasible`、`select_accepted` 等由相应模块实现。特别需要实现：异常捕获和记录、超时取消、候选为空、多个候选不同评价、Budget 原子扣减、Agent 运行环境重建、清晰停止原因。若当前任务的再次执行用于反馈，必须区分该结果与独立验证集的接受依据。

## 12. 插件配置建议

```yaml
experiment:
  id: aer-mvp-smoke
  seed: 42
  runner: aer

benchmark:
  plugin: toy_tasks
  split: dev

agent:
  plugin: react_agent
  model_provider: llm_provider
  max_steps: 8

assessment:
  evaluator: deterministic_evaluator
  diagnoser: llm_diagnoser

routing:
  plugin: fixed_router
  target: prompt

targets:
  allowed: [prompt, memory]
  default_operators:
    prompt: simple_prompt_evolver
    memory: simple_memory_evolver

validation:
  plugin: paired_validation
  split: validation
  min_improvement: 0.01

limits:
  max_evolution_rounds: 3
  max_tokens: 80000
  max_tool_calls: 100

storage:
  experience: jsonl
  artifacts: local_files
  versions: local_manifest
```

插件的类、版本、配置和模型标识应写入每次运行的 `run_manifest.json`，以确保结果可以复现。

## 13. 最终目录结构

```text
AER/
├── aer_core/
│   ├── contracts/
│   │   ├── agent.py
│   │   ├── benchmark.py
│   │   ├── evaluator.py
│   │   ├── diagnoser.py
│   │   ├── router.py
│   │   ├── evolution.py
│   │   ├── validation.py
│   │   ├── runner.py
│   │   └── storage.py
│   ├── schemas/
│   │   ├── task.py
│   │   ├── execution.py
│   │   ├── evolution.py
│   │   └── experiment.py
│   ├── orchestrator.py
│   ├── context_builder.py
│   ├── target_registry.py
│   ├── operator_resolver.py
│   ├── plugin_registry.py
│   ├── plugin_manager.py
│   ├── budget.py
│   └── errors.py
├── plugins/
│   ├── agents/react_agent/
│   ├── routers/fixed_router/
│   ├── routers/rule_router/
│   ├── evolution/prompt_evolver/
│   ├── evolution/memory_evolver/
│   ├── benchmarks/toy_tasks/
│   ├── evaluators/deterministic/
│   ├── diagnosers/llm/
│   ├── validation/paired/
│   └── storage/jsonl_local/
├── integrations/
│   ├── agent_adapters/
│   ├── evolution_adapters/
│   └── baseline_adapters/
├── experiments/
│   ├── runners/aer_runner.py
│   ├── runners/external_baseline_runner.py
│   ├── configs/
│   └── results/
├── tests/
│   ├── contracts/
│   ├── integration/
│   └── e2e/
├── runs/
├── pyproject.toml
└── main.py
```

## 14. 迭代开发任务、测试和验收

### Milestone 0：冻结 Contract（优先）

**开发**：定义 schemas、插件协议、版本约定、异常模型、能力声明、TraceEvent 事件信封。实现 Registry 的注册和实例化。  
**验收**：两个 Mock Agent、两个 Mock Router、两个 Mock Evolver 均可被同一 Orchestrator 调用；插件实现不需要修改 `aer_core`。

### Milestone 1：Agent 执行与评估

**开发**：自研 ReAct Agent、Tool/Model Provider、Trajectory Recorder、Toy Benchmark、确定性 Evaluator、基础 Diagnoser。  
**验收**：可以稳定执行任务、输出 `ExecutionResult`、判定结果、定位日志；替换第二个 Agent 插件后依旧通过接口测试。

### Milestone 2：最小自进化闭环

**开发**：Fixed Router、Prompt Evolver、Target Registry、Operator Resolver、Harness、Artifact/Version Store、Experience Bank。  
**验收**：一次失败任务可触发 Prompt 候选进化、独立验证、接受或回滚、保存完整经验；复测失败后保持稳定版不可变。

### Milestone 3：插件与实验兼容

**开发**：Memory Evolver、Rule Router、统一 Experiment Runner、Benchmark Adapter 和 External Baseline Runner Skeleton。  
**验收**：仅改 YAML 即可在固定/规则 Router 之间切换；可把一个最小外部方法适配成 Baseline Runner，统一输出 `ExperimentResult`。

### Milestone 4：Phase 2 就绪

**开发**：Benchmark 测试/验证划分、日志成本指标、实验种子、结果汇总、失败恢复、运行 Manifest。  
**验收**：同一 Benchmark 同时运行原始 Agent、固定策略、规则路由和 MVP；至少一组外部 Baseline 能通过 Adapter 跑通；确保不同运行的评测条件明确记录。

### 必须有的自动化测试

| 测试 | 核心断言 |
|---|---|
| Contract test | 各插件返回标准类型、错误语义一致 |
| Plugin replacement | 换 Agent / Evolver / Router 不改 Orchestrator |
| Target feasibility | Router 永不接收不可执行的 Target；非法目标被阻止 |
| Stable rollback | 候选失败后稳定版 digest 不变 |
| Experience completeness | 接受与拒绝尝试均写入经验库 |
| Evaluation isolation | 未经授权的隐藏标签不进入 Router/Evolver |
| Reproducibility | 插件版本、seed、任务 split、配置与环境均可追踪 |
| Budget & stop | 达预算、空候选、错误、最大轮数均可正常退出 |
| Baseline adapter | 原生方法输入/输出正确转换，不修改其核心逻辑 |

## 15. 关键风险与规避

1. **过度抽象**：先固定跨模块协议，不要立刻把每个内部函数都做成插件。
2. **Agent 状态不可回滚**：以不可变 Snapshot + 隔离执行为主；不支持的能力明确声明。
3. **Target/Operator 混淆**：Router 决定“进化什么”，Resolver/Operator 决定“怎么进化”。
4. **第三方复现失真**：完整方法对比优先使用 Baseline Runner；组件复用才使用 Evolution Adapter。
5. **评估污染**：开发/验证/最终测试集严格区分；不能让进化方法接触不应访问的答案。
6. **自进化无限循环**：预算、迭代上限、重复目标限制、无收益终止条件必须显式实现。
7. **插件代码执行安全**：外部 Code/Tool Evolver 或未受信任插件应放入受限隔离环境；禁止把动态导入当作安全沙盒。
8. **归因不清**：对照 Router 时固定 Agent、Operator、评价和预算；对照 Operator 时固定 Router 与 Agent。

## 16. 后续研究接口预留

- **Phase 2 / Benchmarks & Baselines**：在统一 Runner 协议下同步评测 Baseline 与 AER MVP，输出同构实验指标。
- **Phase 3 / Adaptive Router**：保持 `Router.select(context)` 不变，替换插件为基于 State、Trajectory、Result、Reason、History 的学习型路由器。
- **Phase 4 / Evolution Genome & RL**：增加 `ContextEncoder` 接口，将结构化上下文压缩为 `G_t`，经验记录整理为 `(G_t,Z_t,O_t,r_t)`，再引入训练组件；不让训练模块耦合核心在线 Orchestrator。

---

## 开发执行建议（最小可行路径）

**第一周优先级不是实现全部九个 Target，而是冻结核心 Contract，跑通 ReAct Agent + Fixed Router + Prompt Evolver + 验证/回滚 + 经验库，并用替换第二个插件的测试证明架构可扩展。** 完成后再接 Memory Evolver、Rule Router、Benchmark 和 Baseline Adapter。全过程以“主框架零业务实现依赖、候选版本不污染稳定版本、实验结果可复现”为三个硬性验收指标。
