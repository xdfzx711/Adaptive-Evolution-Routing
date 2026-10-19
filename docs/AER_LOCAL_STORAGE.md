# AER 本地工件、版本与经验存储

开发步骤 5 已实现。本地工件、Snapshot 谱系、稳定版本指针、验证记录和 JSONL 经验均可持久化，并使用步骤 4 的 Registry/Manager 装配。实际任务执行和验证算法仍由后续 Agent/Harness/Orchestrator 实现。

## 1. 初始化与目录

```powershell
.\.venv\Scripts\aer.exe init-storage experiments/configs/v0_1_fixed_prompt.yaml
```

命令在配置的 storage.root 下创建唯一 run_id 子目录，保存初始 Prompt、Snapshot、稳定指针和 run_manifest.json。它不实例化 Agent、不执行任务，也不调用模型。可以显式指定运行目录名，用相同配置重新打开该目录：

```powershell
.\.venv\Scripts\aer.exe init-storage experiments/configs/v0_1_fixed_prompt.yaml --run-id example-run
```

run_id 采用 1–128 位 ASCII 字母、数字、点、下划线或连字符，以字母/数字开头。已有运行目录只能以相同初始化信息和 manifest 重新打开，不覆盖不同配置。新目录名可隔离不同实验。init-storage 支持 --benchmark-index 和 --output，输出 JSON 文件放在托管运行目录之外。

目录随写入逐步创建：

```text
<storage.root>/<run_id>/
  .storage.lock
  run_manifest.json
  storage_index.json
  episodes/episode-<local-timestamp>.json
  rounds/initial/
    artifacts/<kind>-<local-timestamp>.bin
    versions/snapshot-<local-timestamp>.json
    stable.json
  rounds/<task>-<episode-time>/
    stable.json
    initial/artifacts/<kind>-<local-timestamp>.bin
    round-001/
      record.json
      artifacts/<kind>-<local-timestamp>.bin
      versions/snapshot-<local-timestamp>.json
      versions/candidate-1-<local-timestamp>.json
      versions/commit-intent-<local-timestamp>.json
      versions/commit-receipt-<local-timestamp>.json
      validation/validation-<local-timestamp>.json
      validation/batch-<local-timestamp>.json
      validation/cache/<task>-<local-timestamp>.json
  experience/events.jsonl
```

ArtifactRef、Snapshot RecordRef 和验证 RecordRef 的 URI 相对运行根目录。run_manifest.json 合并原有五份清单，分区保存冻结配置、实际插件版本、初始 Snapshot 引用、任务和流程约定，去掉配置 Schema、工厂路径、重复依赖和全量注册表。

## 2. 工件与 Snapshot

实现位置：plugins/storage/artifacts.py、versions.py。

LocalArtifactStore 使用 kind 和内容确定工件 ID，用内容摘要标识版本；文件名采用 kind 加本地时间，storage_index.json 保存内容身份到物理路径的映射。相同内容重复 put 返回同一引用，不同内容产生新文件；API 不覆盖已有不同内容。get 核对引用身份与文件内容。旧版 artifacts/sha256-*.bin 引用仍可读取，迁移后通过位置映射读取新目录中的原始内容，冻结导入也保留原逻辑引用。时间路径不参与初始 Agent 的内容身份计算。

LocalVersionStore.save 保存不可变 Snapshot manifest，核对工件可读、父版本存在。同一 version_id 的同一内容可重复保存，不同内容报错。load 检查版本身份和 manifest 内容，再返回领域模型的只读嵌套数据。

initialize 保存初始版本并第一次建立稳定指针；重复初始化相同初始版本不会将已经进化的指针重置。stable 返回当前稳定 Snapshot。save 和 materialize 都不切换指针。

materialize 根据已保存父版本和候选工件生成新 Snapshot，记录 candidate_id 到 candidate_version_id 的关联。旧工件、旧 manifest 和原 Snapshot 保留；候选被拒绝时无需执行文件覆盖式回滚。

同一运行目录可用不同 scope_id 建立独立指针，共享工件和版本文件。默认启动采用 default scope；未来独立任务 episode 使用各自 scope，continual 模式沿同一 scope 保留稳定版本。各进化尝试仍使用唯一的 run/episode/attempt/candidate 身份。

## 3. 验证记录与提交

save_validation 持久化完整或不完整的 ValidationReport，检查其候选关联、版本与工件引用；load_validation 通过 RecordRef 重载。保留不完整报告用于分析，但它不具备提交资格。该方法只保存报告，不执行评分。

新 Harness 为验证记录分配时间格式的 validation_id，对应同名 JSON 文件；旧版 ID 继续按原哈希路径解析，已有验证引用无需迁移。

commit 的操作顺序：

1. 核对当前 scope 稳定版本仍等于 expected_stable_version_id。
2. 核对提交 Snapshot 等于已保存的候选版本，候选关联与父版本一致。
3. 加载不可变验证记录，要求 complete 且对应相同候选、旧版本和候选版本。
4. 核对 AcceptanceResult 的分数差与报告一致；接受阈值由独立策略负责，存储不重新定义策略。
5. 写入不可变提交 intent。
6. 原子切换 stable.json，指针同时保存本次提交凭据。
7. 保存 committed 凭据，并追加 VersionCommittedEvent。

一个请求重复提交返回原 VersionCommitRecord，不重新生成版本、不重复追加事件。旧请求在后续进化已提交后重试，也不会把指针回退。复用相同提交身份但改变请求内容会报错。基于过期父版本的另一候选不能提交。

版本库使用同一目录的进程内锁与系统文件锁，串行处理读写和比较后切换；临时文件先 flush/fsync，再发布完整文件。竞争进程无法获取锁时报告 StorageError。当前是本地同步实现，未引入分布式事务或数据库。

## 4. 中断与真实提交状态

稳定指针发布前的写入失败保留旧指针，intent 和候选可在相同请求重试时复用。

稳定指针已发布、提交日志或经验追加尚未完成时，commit 报 CommitFinalizationError；异常的 committed_record 表示已经发生的提交。这种情况不能标记成普通拒绝或将 committed 写成 false。

stable、initialize 和 commit 会核对指针携带的提交凭据，补齐 committed 文件和经验事件。相同事件 ID 的重复追加是幂等的。指针只随同一个被验证的 Snapshot 切换，不在恢复时重新生成 Prompt。

恢复测试覆盖指针发布失败、提交后经验追加失败、committed 文件缺失和旧请求重放；这些是正常文件系统上的中断/故障注入验证，不声明所有平台和介质上的断电一致性。

## 5. JSONL 经验

实现位置：plugins/storage/experience.py。

append 接受步骤 3 定义的五类 ExperienceEvent。进化尝试、候选结果和实际提交分别保存；成功、拒绝、无收益、异常、空候选、验证中断都可作为类型化事件写入，不要求产生候选才能保存尝试。

同 event_id、同内容重复追加返回同一 RecordRef；同 ID、不同内容报错。events 提供完整可读审计记录，包含尚未结束的 attempt_started。get 根据引用读取单个事件。

query 使用 run_id、可选 episode_id/task_id 和 before_or_at。当前时间尚未完成的尝试不进入历史查询；候选与提交事件按 attempt_id 关联已结束尝试。最终任务事件可独立返回，即使没有任何进化。完整原始事件中的私有引用仍由后续 ContextBuilder 筛选，不能直接作为 Router/Evolver 输入。

追加后 flush/fsync。遇到缺少换行的有效最后记录时保留它并在下一次追加时补换行；最后一个未完成 JSON 片段不进入读取结果，下次追加时清除该片段。已经以换行结束的损坏记录会报错并保留文件，不跳过中间记录。

JSONL 当前顺序扫描，无数据库索引，适用于 MVP 的本地实验记录。

持久化的 `.json` 文件采用 UTF-8、两空格缩进和末尾换行；内容身份和摘要继续按原有规范化序列计算，不受显示缩进影响。旧的紧凑 JSON 可继续读取和幂等保存，不改写其不可变正文。经验文件保持 JSONL 的每事件一行格式，人工阅读使用 `evolution.txt` 及 `rounds/<任务名>-<会话时间>/round-NNN/record.json`。每轮一个 JSON 直接展开候选与验证内容，底层记录中的技术引用仍供加载与经验查询使用。

## 6. Python 接口与冻结版本导入

```python
from experiments.storage import prepare_storage

storage = prepare_storage("experiments/configs/v0_1_fixed_prompt.yaml")
stable = storage.version_store.stable()
old_prompt = storage.artifact_store.get(stable.artifacts["prompt"])
```

该入口通过 Registry 构建 local_files/jsonl/local_manifest 三个实例，再建立初始版本。也可直接使用这些类或 PluginManager，以便在测试中提供指定路径和 scope。

最终测试配置设置 evolution.mode=disabled、execution_split=test、initial_prompt=null，并用 agent.initial_snapshot_ref 指向源运行 rounds 下的 Snapshot 文件（旧版 versions/manifests 仍可读取）。初始化新运行时，启动层载入该 Snapshot、复制父版本谱系与工件，再将冻结版本作为初始稳定版本。

导入保持 Snapshot/version/artifact 身份，不依赖源运行指针；新运行的 Agent、模型和工具配置必须匹配冻结版本。持久化 Snapshot 导入已实现，assemble-config 的 Mock 装配仍默认使用内存 fixture；未来 Runner 将实际执行接口与本地存储连接。

## 7. 验收证据

自动化测试覆盖工件重载、旧版本重载、嵌套不可变、拒绝后指针与原 Prompt 不变、无候选失败记录、验证中断保存、重复事件/提交、过期父版本、引用匹配、提交恢复、scope 隔离、竞争写入、时间截止查询及冻结版本导入。

存储测试的验证分数与工件是显式测试数据，用于验证持久化和状态转换；完整的独立验证实验由后续 Harness 提供。

## 8. 旧运行迁移

`python main.py reorganize-run <已完成运行目录>` 只整理本地数据，不装配模型、不读取 .env、不重新评分。工件、候选快照、验证报告、批次、缓存和提交凭据按任务与轮次归档；共享初始内容位于 rounds/initial。episode 清单使用时间编号，其他数据文件使用类型和时间名称。

迁移先在运行目录外的 `.migration-backups` 保留 ZIP，再移动文件、合并清单并逐项验证工件、Snapshot 谱系和稳定提交凭据。失败时恢复原文件；重复迁移不会再次改写。不可变工件、快照和经验流水不改正文，storage_index.json 的 aliases 映射让旧逻辑引用保持可读。内容身份仍使用内部哈希，文件和目录名称不再使用哈希。
