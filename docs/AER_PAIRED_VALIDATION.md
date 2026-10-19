# AER 配对验证与候选接受

步骤 10 已实现。输入是步骤 9 的完成生成记录，输出包含逐候选验证报告、接受原因与唯一选中候选。此步骤不提交稳定版本，也不执行提交后复测。

步骤 11 的 [主控制器](AER_ORCHESTRATOR.md) 已通过进程内服务复用此验证流程，并负责后续提交与复测；独立 validate-proposal 命令继续只验证和选择。

## 使用入口

沿用生成阶段的配置与 run_id：

```powershell
.\.venv\Scripts\aer.exe validate-proposal experiments/configs/v0_1_fixed_prompt.yaml proposal.json --run-id prompt-demo --output validation.json
```

proposal.json 是 propose-prompt 输出的完整报告，也可以是独立 EvolutionProposalRecord。只有成功生成候选的 finished/completed 记录才能进入验证；成功任务的 skipped 记录和空候选记录不进入此命令。当前任务必须来自当前配置的开发任务清单，父版本必须仍是稳定版本，尝试必须匹配已保存的经验。

Qwen 使用 experiments/configs/v0_1_qwen_fixed_prompt.yaml，凭据和模型名继续从 .env 解析。验证用量是 Provider 报告的实际用量；scripted_model 使用模拟用量。完整预算账本仍暂缓。

完整验证后即使候选被普通规则拒绝，退出码仍为 0；执行或评分异常、用量不完整返回 2 并保存验证记录。无效候选在执行前拒绝，记录 invalid_candidate 和 validation_error。配置和输入读取错误也返回 2。输出文件不能覆盖输入、配置、.env、Benchmark 数据或托管运行目录。

同一尝试已经保存完整批次记录时，重复命令返回该记录，不新增调用或事件。返回的 usage 是原批次已发生的成本，调用方不应把重复读取的同一批次再次累计。若只有部分持久化证据、批次尚未保存，则明确报告已有验证结果，保留证据供检查，不自动重复全部调用。

## 通用候选 Snapshot

EvolutionCandidate.artifact_updates 是非空的工件更新映射，key 与引用的 kind 必须一致，可以包含一个或多个组件。领域类型、Version Store 和 Harness 不固定 prompt。

VersionStore.materialize 将更新合并到父 Snapshot 的 artifacts 中，保留其余工件、Agent ID 和运行配置，持久化新版本后填写 candidate_version_id。候选工件与父版本必须有效；执行层检查目标和更新工件是否在 Agent 能力及配置允许范围内。

实际 ReAct Agent 支持 Prompt 与 execution_limits，SingleCall Agent 支持 Prompt；这些约束属于插件能力。步数通过 execution_limits 工件进化，Snapshot.config 保持原样，Agent 从传入 Snapshot 读取有效值。Memory 等后续工件也可复用构造与验证机制。修改模型客户端等非工件状态，需要相应组件的状态与装配契约。

## 执行与配对

ScoredTaskExecutor 通过 AgentPlugin、BenchmarkPlugin 和 EvaluatorPlugin 执行明确的任务、Snapshot 与 RunContext。每次由 Agent 创建新会话；Agent 只接收 TaskView，私有 EvaluationSpec 只交给评分器。普通 execute-task 也使用此公共执行与评分入口。

ValidationPlan 固定当前任务、非空且互斥的 Similar/Regression 两组、顺序和独立任务的 PairSeed。旧版和所有候选共用任务与种子。种子由基础 seed、当前开发任务身份、验证任务身份和重复编号派生，不含 candidate/version ID。MVP 每个任务执行一次，不据此声称统计显著性。

Local 的两侧使用 experiment.seed。当前生成记录没有可核验的原执行 seed，首版重新执行旧版 Local，不把过滤后的执行视图伪造为完整执行事实。此后可以使用条件一致的验证缓存。计划中的 baseline_execution/baseline_evaluation 必须同时提供或同时为 null；提供时检查身份，当前 Harness 仍以重新执行或验证缓存获得对照。

不调用 Diagnoser，不更新稳定指针，不把验证任务记录写成开发任务完成事件。Provider 不支持 seed 时将能力写入条件记录，相同请求种子不代表远端输出确定。

## 指标与接受规则

LocalValidationMetrics 保存旧/新分数和成功标志、delta_score、repaired。Local 不进入独立验证统一分数或 reward，也不是首版接受的硬条件。

独立统一分数是两组并集中所有任务的平均分，按任务数加权，不直接平均两组均分。分别保存 Similar/Regression 分数、成功率和旧版成功而候选失败的回归任务数。baseline 的该计数为 0，candidate 的计数由逐任务配对计算。

AcceptancePolicy 只读取报告与 ValidationSettings，不执行或提交。完整报告须有 Local 与两组证据、配对指标和完整 usage。通过条件是：

```text
delta_score > 0
delta_score >= validation.min_improvement
delta_regression_score >= -validation.max_regression_drop
```

采用 Decimal 比较配置阈值，包含边界等号。常见 reason 为 improved、no_improvement、insufficient_improvement、regression_guard、validation_incomplete、validation_error。

完整有效对照保留 reward=delta_score，包括无提升、回归超限和未选中的候选。没有完整验证或实际用量时 reward 和各 delta 为 null。答错、格式错误和可评分的步骤上限是普通任务结果；执行、工具和评分基础设施异常不填 0 分代替。部分轨迹、错误及已发生用量仍保存。

## 缓存与成本

缓存只保存 complete 的旧版 ScoredExecution，保存在首次执行该旧版验证的轮次目录 validation/cache 下。key 包含完整旧版 Snapshot、公开任务、seed，以及模型/工具/Agent/Benchmark/Evaluator 的插件版本、模型运行信息、任务集合与划分、私有评分规格版本和内容、调用参数等评测条件。条件变化不能复用旧结果。

同批次只执行一份旧版 Local 与独立验证。缓存命中保留原始结果及其 operation ID，并在 PairedTaskResult 中标记 baseline_cached；不重复计入原调用成本。

每个 report.usage 是本批次实际归属到它的成本，共用旧版的新调用归属到返回序列第一份报告一次，其余报告只承担候选成本。批次 usage 累加这些报告；报告中的逐任务证据保留旧版历史 usage，不能再次累加。

candidate_validation_tokens 只计该候选 Local 和独立验证的输入加输出，不含旧版、候选生成或其他候选。未知用量保持 null，不作为已知 0。

## 多候选与存储

报告和接受结果按 candidate_id 建立对应，拒绝重复、缺失和未知 ID。所有通过策略的候选 accepted=true，按统一分数降序、候选验证 tokens 升序、candidate_id 字典序升序选一个。

选中结果为 selected，其他通过者为 accepted，普通不通过者为 rejected，未完成者为 incomplete。所有结果 committed=false。无效工件或不兼容能力在构造阶段留下拒绝和 validation_error，即使没有有效验证报告。

持久化内容：

```text
<run>/
  run_manifest.json           # 一份精简的配置、插件和流程清单
  storage_index.json          # 内部身份与物理位置映射
  rounds/<task>-<episode-time>/round-001/
    artifacts/                # 当前任务和独立逐题证据、批次工件
    versions/                 # 候选快照、关联和提交凭据
    validation/               # ValidationReport 与验证批次
      cache/                  # 完整旧版缓存，后续轮次可复用
    record.json               # 一份直接展开内容的阅读日志
  experience/events.jsonl     # 尝试、评估和真实提交事件
```

逐任务验证输入、评分规格、反馈和结果均留在评估侧。ContextBuilder 只读取允许的聚合收益和接受/选择/提交标志，不转发验证私有工件。

Python 入口：

```python
from pathlib import Path
from experiments.validation import prepare_validation, load_proposal

prepared = prepare_validation("experiments/configs/v0_1_fixed_prompt.yaml", run_id="prompt-demo")
batch = prepared.validate(load_proposal(Path("proposal.json")))
payload = prepared.save_record(batch)
```

工程验收覆盖两个实际 Agent、非 Prompt 工件、配对 seed、按任务数加权、Local 修复与独立收益分离、阈值边界、回归计数、缓存失效、多候选顺序打乱、异常与未知用量、私有反馈隔离、经验更新、CLI 往返和稳定版本隔离。测试中的改善模型是工程 fixture，不代表研究收益。
