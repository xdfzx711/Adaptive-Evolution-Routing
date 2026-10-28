# GEPA Prompt Evolver

`gepa_prompt_evolver` 是独立 EvolutionPlugin，使用官方 `gepa==0.1.4` 的 `optimize()`、反思提示词生成和 Pareto 候选搜索。现有 `simple_prompt_evolver`、Model/Tool Evolver、各 Router 和主控制器保留原接口。没有新增固定路由组合配置，也没有改变现有实验配置的默认 Operator。

官方项目：https://github.com/gepa-ai/gepa

## 安装和启用

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[gepa]"
```

无需克隆 GEPA 源码。未选择此插件时，不要求安装 GEPA。选用插件时检查版本，防止库升级改变算法行为而没有记录。

在任意兼容实验配置中，将 Prompt 的 Operator 改为该插件；保留自己的 routing、其他 targets 和实验语义。以下仅为要合并到现有 YAML 的字段片段：

```yaml
targets:
  # allowed 和其他 default_operators 沿用实际实验配置
  default_operators:
    prompt: gepa_prompt_evolver
  operator_configs:
    prompt:
      max_metric_calls: 8
      samples_per_evaluation: 3
      max_iterations: 3
      reflection_minibatch_size: 1
      use_merge: false
      skip_perfect_score: false
      timeout_seconds: 600.0
      trial_timeout_seconds: 300.0
      reflection_timeout_seconds: 60.0
      reflection_max_output_tokens: 4096
      reflection_model:
        plugin: qwen_model
        model_id: ${GEPA_REFLECTION_MODEL}
        config:
          api_key_env: GEPA_REFLECTION_API_KEY
          base_url_env: GEPA_REFLECTION_BASE_URL
          dotenv_path: ../../.env
          temperature: 0.0
          enable_thinking: false
          response_format: null
```

`dotenv_path` 相对 YAML 所在目录解析，示例路径适用于 `experiments/configs/`。`.env` 中填写：

```dotenv
GEPA_REFLECTION_API_KEY=...
GEPA_REFLECTION_BASE_URL=...
GEPA_REFLECTION_MODEL=qwen3.8-max
```

反思模型通过注册的 ModelProvider 独立构建，不复用任务模型或诊断器的运行实例；插件代码不依赖 Qwen SDK。默认采用 Qwen 环境变量，也可配置其他已注册的 ModelProvider。API key 不进入 Snapshot、配置清单、搜索记录或反思请求日志。

## 模型与算法职责

- Codex 等执行 Agent 及实际任务模型由当前 Snapshot 控制。内部试跑只替换 Prompt，保留 Model、推理强度、Tool 和其他组件。
- 当前 Codex 实验基准为 `gpt-6.1-sol`，用于补丁生成、内部试跑和外层验证；当前 SWE-bench GEPA 配置的反思 Provider 读取 `.env` 的 `QWEN_MODEL=qwen3.8-max`。单独配置 `GEPA_REFLECTION_MODEL` 时也可使用同一文本模型，API 依赖仍独立装配。
- GEPA 的反思模型读取公开执行证据，生成提示词；保留官方提议逻辑，并追加可复用指令和反馈权限约束。
- JEV、其他 Router 和诊断器保持现有模式。GEPA 不读取 Router 类型来选择算法分支。
- 一次外层进化调用可以包含多次内部试跑和反思；默认最多向 AER 返回一个 Prompt 候选。
- GEPA 内部接受表示进入搜索候选池，不表示 AER 接受或提交。最终候选仍经过外层配对验证和 AcceptancePolicy。当前任务是否成功仍由稳定版本的 Evaluator.success 判断。

## 隔离与数据权限

`CandidateTrialService` 是可注入的窄接口。默认实现为每次试跑构建新的 Agent、Benchmark、Evaluator、Provider 和工具依赖图，并使用独立临时工件 Store、运行标识与工作区。临时目录在试跑后释放；不调用外层 Orchestrator、诊断器、Router、验证缓存、经验写入或版本提交。

Evolver 收到的 Snapshot 配置经过公开权限过滤。可信试跑服务通过只读版本加载函数读取该精确 version_id 的完整冻结配置，核对公开视图后再创建临时版本；不会读取可变 stable 指针来替换请求父版本，也不会把完整运行配置或评分规格交给 GEPA。

Codex 支持可选的 RunContext.execution_workspace_root 和 execution_timeout_seconds；未提供时沿用原行为。仓库准备插件在隔离目录下建立内部仓库缓存，可只读使用已有基线仓库作为 fetch 来源，不写外层仓库缓存。这里提供实例、状态、目录及数据权限隔离，不是操作系统权限沙箱。

首版默认训练与内部选择都使用当前开发任务，并在日志标记 `selection_reuses_training=true`。可显式配置 `train_task_ids` 和 `selection_task_ids`，但只能选择当前实验选中的开发任务；`state.task_mode=independent` 下只允许当前任务。跨开发任务优化需要显式使用 continual 模式。不能选择外层 validation/test 任务。

每个 Prompt 在每个任务上默认固定执行 **3 次**（`samples_per_evaluation=3`），使用算术平均分参与 GEPA 初筛和内部选择集排名，不取最高分。每次采样仍使用独立会话和工作区；所有 Prompt 使用相同的 `seed, seed+1, seed+2` 请求种子序列，Agent 不支持种子时不保证可复现。反思模型能看到全部三次公开结果、轨迹、逐次分数及平均分、标准差，而不是只看到最好的一次。

一次 GEPA 搜索内，相同 Prompt、任务、Snapshot 与执行条件的完整采样组会被缓存。初始选择集评测、父候选反思试跑、子候选试跑和后续选择集排名命中同一组时，直接复用分数和轨迹，不再次执行；选择集包含不同任务时仍分别采样。缓存不跨外层轮次或运行，也不使用外层执行/验证缓存。

试跑服务用相同的 PublicFeedbackBuilder 过滤任务元数据、输出、轨迹和评分反馈。GEPA 反思不接收 Evaluator 的原始私有 prose、metrics、参考补丁、评分规格或外层验证任务正文。当前 SWE-bench 实验仍是静态补丁评分，不执行项目测试。单任务接入不代表复现了 GEPA 论文的多任务实验结果。

## 轮数、停止与成本

- `limits.max_evolution_rounds`：外层轮数。
- `max_iterations`：一次 GEPA 调用的内部搜索轮数。
- `samples_per_evaluation`：每个 Prompt/任务的固定采样次数，默认 3；完整组的平均分用于比较。
- `max_metric_calls`：一次 GEPA 调用实际执行、评分的任务次数上限，每次采样计一次，缓存复用不计次；不是反思模型调用数量。由插件按真实试跑记录检查，避免 GEPA 自身的任务示例计数与重复采样数量混淆。
- 当前 SWE-bench 配置保留 `max_metric_calls=8`、`max_iterations=2`。单任务下原 Prompt 三次、新 Prompt 三次，共六次；剩余两次不足以开始下一组时以 `metric_limit` 停止，不使用不完整组。若要评测原 Prompt 加两个新 Prompt，可显式将额度调整为 9。
- `deduplicate_prompts` 默认 true：同一个任务 episode 内，插件记住已生成提示词；忽略换行编码、行末空格和首尾空白的差异。重复候选在试跑前拦截，正常的父候选与起始 Prompt 重测不受影响。
- `max_duplicate_retries` 默认 1：重复时向反思模型说明首次出现的外层轮次，允许一次重新生成。仍重复则以 `duplicate_prompt` 正常停止内部搜索，保留已完成的最佳新候选；没有新候选时返回 empty。重试消耗计入原有反思日志和 usage。
- 去重记忆属于 Evolver 插件实例，作用域为 run、episode、任务和执行组件。模型、工具或受控 Agent 配置改变后允许重新尝试相同文本；不同任务与不同运行不共享去重记忆。不改变框架经验库或增加日志文件；当前框架不支持重启后续跑同一 episode。
- `timeout_seconds`：内部搜索时限，在执行边界检查；反思和任务调用使用剩余时限约束。任意第三方同步插件仍需遵守自己的超时接口。
- 预算或内部轮数不足以完成新候选的内部选择集评测时，不导出该未完成候选。若已有完整评测的最佳候选则保留；最佳仍为起始 Prompt 时返回空候选。
- 内部执行/评分异常和不完整 usage 不伪装成 0 分；反思失败也保留调用与消耗，并返回标准错误。适配层阻止 GEPA 的反思 fallback 重复发起已经失败的模型请求。
- 每次调用从本轮稳定 Prompt 开始，不跨 Model/Tool 配置变化复用旧搜索分数。内部所有执行、评分与反思 usage 在 Evolver 中汇总一次。

## 日志

沿用现有目录、时间格式和候选序号，不启用 GEPA 的独立 run_dir、WandB、MLflow 或进度条文件：

- `evolution.txt`：以 `[时间] [内部搜索/内部试跑/内部汇总/内部复用/内部反思]` 显示进度；`【Evolver 内部优化】` 汇总内部候选 C1/C2、父子关系、内部选择平均分、采样汇总、复用、接受/拒绝、逐次试跑、反思次数、原因和成本。
- 当轮 `record.json` 的 `generation.internal_optimization`：展开完整内部候选、逐次公开执行轨迹、反馈、反思请求与响应、选择记录和成本，JSON 两空格缩进。
- 当轮 artifacts：保存一个不可变 `evolution_optimization-时间.bin` 收据，供完整结果关联和离线报告重建。`EvolutionResponse.record_ref` 为可选字段，旧插件和旧记录不受影响。空候选和异常也保存收据。

内部实际试跑记录有独立运行标识，但阅读报告只展示时间、试跑序号和候选序号，不要求读者追踪内部版本哈希。

当轮 `decisions` 中的 `sampled_evaluation` 包含每次分数、请求种子、试跑序号、平均分与总体标准差；`evaluation_reused` 关联到同一组试跑，不重复记成本。采样中断或失败会保留已完成执行及其成本，但不会用不满三次的均值给候选排名。

## 验证

```powershell
.\.venv\Scripts\python.exe -m unittest tests.evolution.test_gepa tests.integration.test_gepa_startup -v
```

这些测试使用真实 GEPA 优化引擎、离线模型 Provider，以及现有 Agent/Codex CLI 协议 fixture；覆盖隔离、模型继承、公开反馈、空候选、错误成本、外层提交和离线日志重建，不会调用真实远端模型。
