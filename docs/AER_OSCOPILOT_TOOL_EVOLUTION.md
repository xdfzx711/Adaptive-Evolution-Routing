# OS-Copilot 工具演化插件

`oscopilot_tool_evolver` 通过现有插件注册接口接入 AER，适配器版本为 `2.7.0`。工作区、模型、工件存储和进度报告通过依赖注入提供；AER 核心不依赖 OS-Copilot 的内部工具格式。

## 工具集与提交边界

| 对象 | 内容与用途 | 生效规则 |
| --- | --- | --- |
| 构造临时库 | 独立工作区内的原生 ToolManager，包含辅助工具、中间结果及已有工具副本 | 不直接发布给正式 Agent |
| 候选工具集 | 生效工具集副本，加上通过导出适配与复验的新增/替换工具 | 仅候选验证执行装配该版本 |
| 生效工具集 | 当前稳定 Snapshot 引用的工具工件 | 仅 AER 验证、接受并提交后更新 |

```text
生效工具集 → 独立构造环境中的副本
                  ↓
OS-Copilot：规划 → 生成 → 执行 → 评判 → 修复/重规划 → 临时入库
                  ↓
选定构造产物 → Codex 元数据转换 → 干净工作区复验 → 冻结候选工具集
                  ↓
AER：当前、相似、回归任务验证 → 接受策略 → 提交新生效版本
```

拒绝候选、导出失败或交付复验失败均保留原生效工具。候选验证期间源码冻结；每个版本的 MCP 工具来自自己的 Snapshot。候选包含完整工具集；`max_generated_tools` 限制本轮成功转换的工具数量，不限制规划或内部构造节点数量，也不限制整个稳定工具库大小。转换失败不占名额；交付复验失败还可能进一步减少最终候选中的工具数量。

## 构造与转换分离

`engine.py` 组织原生规划、执行、评判、修复和工具库调用，返回 `ConstructionResult` 原始产物。`repository.py` 中的 `ConstructionRepository` 明确表示临时库。

构造使用原生 Python 代码块和 `<invoke>`，无需 `<tool_spec>` 或完整发布元数据。插件调用原生 `FridayExecutor.execute_tool`，由 `native_runtime.py` 注入有界执行环境。代码及调用表达式实际执行，保留 stdout、错误与返回值；内部非 JSON 返回可表示为文本，正式导出工具仍必须返回 JSON。

执行事件完整保留 result、stdout 和错误。`output_view.py` 只为后续节点及元数据转换提供有界视图：从 stdout 删除与返回值完全相同的独立打印内容，保留其他日志；后续节点的结果预览阈值为 12,000 字符，元数据转换为 4,000 字符，日志和错误各使用该阈值的四分之一。超过阈值时明确提供 truncated、原长度、类型、部分键名及 JSON 文本前缀，不声称是完整结果。full_output_ref 定位构造记录内的完整执行事件，是记录索引，不是模型可自动读取的文件路径。工具源码、原始记录和当前节点的执行 state 保持完整；后续节点接收的前置结果改为上述视图。原生评判自身的输出截断策略保持不变。

`response_format.py` 在本插件的 executor 实例上替换响应提取接口，生成和修复共享同一解析器，原生 generate_tool、repair_tool、execute_tool、judge_tool 的策略保持不变。独立调用标签必须包含合法 Python 调用表达式；说明文字中的行内标签和代码字符串中的标签不视为调用。代码围栏缺失结束标记时，只有独立调用边界前的源码完整、语法可解析才自动分离；已经有完整围栏的源码即使有语法错误，也原样进入原生执行与修复。

存在多个代码或调用候选时，最多按 max_response_format_repairs 请求模型选择原响应中的候选编号。请求只携带原响应候选及解析错误，不重复完整任务轨迹；模型无法返回新源码或改写算法。缺少可识别代码/调用时不猜测，恢复失败记录 response_format_failed，停止该构造分支并形成下一轮反馈；没有其他可交付产物时返回 empty。格式恢复不占原生代码修复次数，但计入本轮调用、token 和时间预算。其他超时、预算、传输错误仍遵守既有停止策略。

内部执行复用独立 worker/Jupyter 传输，具备进程超时、实际仓库 cwd 和凭据环境过滤。每次调用拥有新 Python 命名空间，提供 workspace 全局字符串供调用表达式使用；不能依赖前一内核的变量。独立进程/cwd 不等于操作系统文件系统沙箱。

原生 Planner 输出节点图；插件校验原生字段、节点名、依赖、环、类型和图节点数量。deliverable 和 interface 都是可选发布提示，不作为构造前置条件。缺失或非布尔的 deliverable 视为未指定；QA 始终不导出；非对象 interface 忽略。非法原生图结构仍明确报错，没有额外模型规划审核。

候选转换只接收执行成功且原生评判为 Complete 的 Python 产物：显式 deliverable=true 的优先，未标记的按完成时间倒序尝试；显式 false 的内部辅助项不导出。删除根据依赖图末端推断最终工具的逻辑，后续测试节点不会排除已完成工具，也不新增语义分类、正则匹配或规划审核。提示 Planner 将纯验证脚本标为 false，但不强制提供发布字段。未标记产物可能包含辅助代码，进入转换不代表已经适合交付，仍需通过现有 JSON 接口转换、独立工作区复验和外层验证。转换阶段执行数量上限，超额仅记录 export_skipped，不中断构造。

原生模型决定 Complete/Amend/Replan；达到通用性门槛的成功工具进入临时库。min_generality_score 只控制临时入库，不再控制 AER 候选导出：执行完成且被选中的 7 分工具仍可转换、复验。评分记录注明 score_scope=construction_library，反馈注明是否阻断候选导出。模型意见和诊断假设仍属于假设，不应当作已证实缺陷。

`delivery_guidance.py` 为本插件的规划/重规划、生成和修复提示提供同一份交付意图：根据实际缺失能力设计可复用操作，公开参数接收调用者的数据或仓库路径，结果依赖这些输入或当前工作区。固定示例、演示和验证脚本与公开 callable 分开；按预期业务结果验证行为，不以可序列化或类型正确代替算法正确。允许确实只依赖当前工作区的无参工具，辅助节点仍可使用原生内部形式。这是提示引导，不是新增规则检查、模型审核或交付门槛，也不修改上游评判/修复逻辑。

`exporter.py` 在构造结束后完成转换：

- 读取已有合法元数据，兼容标准标签和响应末尾裸 JSON；说明文字中的标签不会被当成 JSON。
- 没有元数据时，从简单函数签名、调用表达式与规划接口推导；不执行代码猜参数。
- 无法确定时，按 max_export_metadata_repairs 限次请求模型补齐元数据。AST 提供同步顶层入口及真实参数，局部函数不能声明为入口。明确只返回函数对象的 builder 记录 incompatible_callable，交由后续原生构造处理，适配层不提升嵌套函数、不改源码。
- JSON 解析失败时，另有最多一次轻量格式恢复（max_export_format_repairs），只发送原响应、解析错误及 schema；再次失败记录 metadata_json_invalid，停止该产物的转换。格式恢复与原生修复次数独立，共享本轮时间、token 和模型调用预算；没有余额时不强行重试。字段/签名不符分别记录 metadata_schema_invalid、metadata_entrypoint_mismatch 或 metadata_signature_mismatch，并进入下一轮构造反馈。
- export_response_format=auto 时，仅元数据请求使用 Qwen JSON Object 模式；其他模型保留文本协议与本地校验。可设 text 兼容不支持 JSON 模式的代理服务。这里不使用服务端严格 JSON Schema 子集来限制作为字段值传递的任意工具 schema。无论服务端格式如何，本地字段、源码接口及独立工作区交付检查仍保留。
- 元数据提示要求简短操作说明、每项一句的 usage、最小调用样例及紧凑输出 schema；禁止把演示数据展开为巨型 schema、复制执行输出或重复前置分析。描述如实区分固定演示与可参数化工具。
- 元数据请求关闭自动附加完整开发证据，只包含源码、调用表达式、描述、接口提示、有界执行样例、格式错误及响应 schema。规划与构造仍接收公共开发证据。超长的错误及失败元数据响应也明确截断，避免格式重试扩大上下文。
- 工具说明以具体操作、实际输入和可用输出开头；usage 说明使用时机、相比普通读取/搜索节省的工作、返回字段如何辅助下一步决策、限制与副作用。参数说明解释如何提供真实数据，示例调用实际公开入口。描述必须忠实于源码：固定演示程序不能被包装成接收任意业务参数的工具。现有 MCP 描述组装会展示这些字段，无需改动通用工具层或强制 Agent 调用。
- 规划 interface 作为提示；补全以实际完成的 callable 和执行结果为准，不因规划接口与最终实现不符而禁止纠正元数据。最终调用仍必须通过独立工作区复验。
- 原生成功执行版源码保持不变；转换只处理描述、入口、参数、JSON schema、usage 和 runtime_outcome。
- 描述中的当前构造根目录规范化为 `<workspace>`，不把原规划的临时绝对路径重新覆盖进去。代码、默认值或真实参数中的路径依赖不会被静默改写。
- 转换失败记录 export_failed；不回流原生代码修复。同批其他合格工具仍可继续形成候选。

构造提示要求最终工具直接定义并调用可复用入口，保持源码自包含。仅写出另一个 Python 文件的 builder 不等于已运行文件内的工具；插件目前导出原生执行的代码块，不自动收集其写出的文件或重写其算法。临时文件依赖、硬编码工作区和非 JSON 接口仍可能使导出或复验失败，并形成后续轮次反馈。

`evolver.py` 返回标准 EvolutionCandidate，仅更新 tool 工件，并标记 tool_set_state=candidate、activation=requires_aer_validation_and_commit。插件不调用稳定版本提交接口；生效仍走 AER 原有验证与版本机制。

## 交付接口与复验

候选工具为同步函数或可调用类，源码、入口、构造参数、描述随工件保存。公开输入为 JSON object；workspace 是运行时保留参数，不出现在公开参数或构造参数中。

仓库路径使用 `{"type":"string","x-aer-path":"repository"}`，支持内联嵌套属性和数组。导出规范化为相对路径，交付与 MCP 调用重新绑定当前工作区；源码文本和普通说明不标记为路径。

最终元数据包含输入/输出 schema、usage、runtime_outcome 和 acceptance。JSON 格式校验不证明分析结论正确；返回 false 或空 findings 可以是成功分析。补丁产物声明 acceptance.kind=patch 与 patch_pointer，复验检查非空补丁并在干净副本中执行 git apply --check。

构造工作区释放后，delivery.py 为每个工具准备全新任务副本，重放 JSON 示例并检查调用、返回格式与声明的成功条件。失败更新不进入候选，同名生效实现保留；没有额外模型评分或自动代码改写。实际任务收益由后续 AER 配对验证判断。

## 源码、配置与运行

上游位于 submodule `third_party/OS-Copilot`，固定提交 `f720af8807e49a92dda64572d2c6bc6c0ac7ee7e`。启动检查版本及工作树，保留上游源码和许可证。source.py 显式列出依赖注入；FridayPlanner、FridayExecutor、FridayRetriever 与 ToolManager 方法直接从固定源码加载。

插件临时工具库使用确定性词项描述索引，记录 retrieval=lexical_description；独立 baseline 保留 Chroma/embedding，二者不是相同检索配置。

```powershell
git submodule update --init third_party/OS-Copilot
.\.venv\Scripts\python.exe -m pip install -e '.[oscopilot]'
```

Agent 需启用 tooling.generated_tools.enabled，运行后端为 python_jupyter。python_executable 可指定已安装 Jupyter/IPykernel 的解释器；tooling.tool_timeout_seconds 限制单次执行。

| Operator 字段 | 默认值 | 含义 |
| --- | --- | --- |
| source_path | 项目 submodule | 也可指定相同提交的干净 checkout |
| generation_model / judge_model | 框架模型 / 生成模型 | 可单独指定 provider |
| max_repair_iterations / max_replan_iterations | 3 / 2 | 原生单节点修复 / 本轮重规划次数 |
| max_generated_tools / max_plan_nodes | 3 / 12 | 本轮成功转换工具 / 图节点上限 |
| min_generality_score | 8 | 原生成功工具的临时入库门槛 |
| max_export_metadata_repairs | 1 | 构造后元数据补齐次数，不占原生修复次数 |
| max_export_format_repairs | 1 | 每个产物最多一次额外 JSON 格式恢复，可设 0 关闭 |
| export_response_format | auto | 仅 Qwen 元数据请求启用 JSON Object；text 使用文本协议 |
| max_response_format_repairs | 1 | 每份生成/修复响应的格式歧义恢复上限，可设 0 禁用 |
| export_token_reserve | 0 | 动态导出预留上限；没有已完成且可选的最终产物时预留为 0，须小于 max_tokens |
| max_example_chars | 2048 | 最终导出参数示例字符上限 |
| timeout_seconds / model_timeout_seconds | 300 / 60 | 整轮预算 / 单次模型超时 |
| max_model_retries / retry_backoff_seconds | 2 / 2 | HTTP 超时恢复次数 / 退避秒数 |
| max_output_tokens / max_expanded_output_tokens | 4096 / 16384 | 初始输出预算 / 截断恢复上限 |
| max_output_expansions | 1 | length 后扩容次数 |
| max_model_calls / max_tokens | 20 / 20000 | 本轮共享调用与 token 预算 |

旧配置需删除 verify_plan_contract、max_plan_contract_repairs，将 max_plan_metadata_repairs 替换为 max_export_metadata_repairs。仓库配置已同步。超时和 length 恢复保留原有预算控制；工作区准备、导出及复验均计入整轮预算。

模型请求前按输入字节数估算输入 token，并用已观察到的请求使用量向上校准；请求记录 estimated_input_tokens、export_reserved_tokens 和 judgment_reserved_tokens。开始生成或修复前，检查“本次完整请求＋后续原生评判”的 token 及模型调用名额，避免只够生成却不够评判。评判输入按本次输入、生成输出上限和已有评判记录估算；格式恢复也保留后续评判额度。上述额度不计入实际消耗，实际消耗仍按模型回执与超时待结算项记账。估算不代替服务端实际计费，也不是精确 tokenizer。

导出预留在构造节点之间动态更新，计算已经通过原生 Complete 评判且未明确排除的产物；显式 false 的辅助节点完成不启用预留。预留与实际首个转换请求共用 initial_metadata / metadata_request，统一包含系统提示、源码、调用、接口提示、有界执行样例、校验错误及 schema，再加输出额度，并以 export_token_reserve 为上限。元数据已完整时无需模型转换，也不预留转换 token；重试内容变化后仍按实际请求检查预算。当前工具的 judgment 不扣导出预留，优先完成执行后的评判；export_metadata 也可以使用预留。评判后释放本次评判额度。

已有可转换产物但不足以开始下一次构造与评判时，结束构造并转入已有产物导出；没有可选产物则报告 budget_interrupted，不把未经原生评判的工具当作已完成。plan_finished.completed 仍如实记录整个原生计划是否完成，未执行的验证节点不会标为完成。真实 token、时间、外层预算及模型调用上限仍有效。实际耗尽文案报告 charged/limit；只有存在待结算超时预留时才附加 pending_timeout_reservations。预留不是额外额度，也不保证所有节点都能完成。

16263 配置的单轮总上限为 **400,000 tokens**，动态导出预留上限为 **50,000 tokens**，没有最终产物时实际预留为 0。其他实验配置保持各自上限。

16263 配置为固定 tool、最多 3 轮、每轮最多导出 2 个工具，相似任务 15375，回归任务 16032：

```powershell
.\.venv\Scripts\python.exe main.py run-task experiments/configs/v0_1_codex_swebench_oscopilot_django_aggregation_llm_eval.yaml `
  --task-id django__django-16263 `
  --output runs/django-16263-oscopilot-candidate-export-result.json
```

外层 llm_patch_evaluator 为轻量模拟评分，正常一次模型调用，JSON 错误最多一次格式恢复。resolved/partial/unresolved 对应 1/0.5/0，未运行项目测试。参考补丁和私有评语不提供给演化器；公开任务、轨迹、结果、诊断、历史与原始 Agent prompt 仍进入内部模型上下文。初始成功按配置直接停止，不强制演化。

## 记录与观测

generation.tool_construction 保存原始响应、源码、执行、评判、入库、导出和复验记录。stored.scope=construction 表示临时入库，export_selected 表示选定原生产物，candidate_tools_prepared 表示转换后候选内容，export_verified 表示交付复验通过；它们都不代表稳定版本提交。

调用阶段包括 planning、analysis、construction、judgment、repair、replanning、response_format、export_metadata、export_metadata_format。导出耗时单列 export_seconds。construction_feedback 将格式、执行、导出和交付失败写入后续历史，包含原始响应或 source/invoke 和具体原因；模型意见仍标记 model_assessment。response_format_recovered 区分本地边界恢复与模型选择次数；construction_budget_reserved 标记提前转入导出的预算原因。旧规划审核事件保留读取兼容。

工具回执区分 discovered、实际调用、执行成功和业务成功；零调用不能归因于工具执行效果。验证按当前/相似/回归任务与 baseline/candidate 分组统计。empty 继续下一轮，error 或 budget_interrupted 保留现有停止策略。

## 原生 baseline

baseline 使用单独安装上游依赖的 Python 环境，直接启动原始 quick_start.py，保留原生规划、执行、评判、修复、入库与 Chroma 检索。

```powershell
.\.venv\Scripts\python.exe -m experiments.baselines.oscopilot --python D:/envs/os-copilot/python.exe --query-file D:/tasks/query.txt --workspace D:/tasks/baseline-workspace --output-root runs/os-copilot-native-001 --dotenv D:/tasks/os-copilot.env --prepare-only
```

移除 --prepare-only 执行。输出目录必须是任务工作区和上游源码之外的新目录，凭据采用上游环境变量，embedding 服务须可用。每次独立工具库不跨题积累。baseline.json 记录原始提交和空 source_patches；退出码 0 仅表示 process_completed，task_success 与 usage 保持 null，需外部评分。

## 验证

```powershell
.\.venv\Scripts\python.exe -m unittest tests.evolution.test_oscopilot tests.evolution.test_oscopilot_response_format tests.evolution.test_oscopilot_planner tests.evolution.test_oscopilot_exporter tests.evolution.test_oscopilot_delivery tests.evolution.test_oscopilot_model tests.integration.test_oscopilot_startup tests.integration.test_oscopilot_baseline -v
```

受控模型响应驱动真实原生方法、本地 worker 和 AER 验证/提交链路，覆盖无元数据执行、原生修复、导出失败隔离、候选接受/拒绝、版本恢复、路径绑定、超时及成本记录。tests/fixtures/oscopilot/missing_deliverable_plan.txt 保留 16263 第二轮失败时的原始规划响应；completed_tool_before_validation_plan.txt 保留 12:53 运行中工具后接验证节点的原始规划。回归测试确认缺少 deliverable 不阻断构造，预算预检停止后已完成产物仍可进入候选转换，复验失败则不产生候选；构造响应为受控测试数据。真实模型的工具质量与任务收益需另行实验。

repair_format_1.txt 和 repair_format_2.txt 保留 2026-11-01 12:08 运行中的两份原始修复响应，只做离线提取和语法检查，不执行其中指向旧临时目录的文件写入。另用受控源码驱动真实 worker，验证源码语法错误进入原生修复，修复响应缺少围栏且正文提及调用标签时仍可执行、导出并通过交付复验。
