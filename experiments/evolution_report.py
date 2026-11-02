"""Deterministic human report over domain records and their immutable evidence."""

import difflib
import json
import re
from datetime import datetime, timedelta
from pathlib import Path

from aer_core.schemas.paired import ValidationEvidence
from aer_core.usage import sum_usage


def plain(value):
    return value.model_dump(mode="json") if hasattr(value, "model_dump") else value


def compact(value):
    return json.dumps(plain(value), ensure_ascii=False, separators=(",", ":"))


def weighted_gain_lines(value):
    gain = value.weighted_gain if value else None
    if gain is None:
        return []
    lines = []
    for name, label, delta in (("current_task", "当前任务", value.delta_current_task_score),
                               ("similar", "同类迁移", value.delta_similar_score),
                               ("regression", "回归变化", value.delta_regression_score)):
        lines.append(f"{label}贡献：{getattr(gain.weights, name):g} × ({delta:+.4f}) = {getattr(gain, name):+.4f}")
    lines.append(f"综合收益：{gain.total:+.4f}（独立验证变化另行记录）")
    return lines


def moment(value=None):
    value = value or datetime.now().astimezone()
    return value.astimezone().strftime("%Y-%m-%d %H:%M:%S %z")


def excerpt(value, limit=600):
    text = str(value)
    if len(text) <= limit:
        return text
    half = limit // 2
    return text[:half] + f"\n[节选：省略 {len(text) - limit} 字符，完整内容见本节原始记录路径]\n" + text[-half:]


def score(result):
    return f"{result.evaluation.score:.3f}" if result and result.evaluation else "未评测"


def termination(result):
    if result.error:
        return f"{result.error.stage.value}/{result.error.code}"
    if result.execution:
        reason = result.execution.state_summary.get("termination_reason", result.execution.status.value)
        if result.evaluation and not result.evaluation.success:
            return f"{reason}/{result.evaluation.reason_code}"
        return str(reason)
    return "无执行结果"


class EvolutionTextReport:
    """One episode section. Versions/candidates have readable aliases within it."""

    def __init__(self, write, artifacts, versions, *, root=None):
        self.write, self.artifacts, self.versions = write, artifacts, versions
        self.root = Path(root) if root is not None else None
        self.current_time = datetime.now().astimezone()
        self.version_names = {}
        self.candidate_names = {}
        self.sources = {}
        self.readable_sources = {}
        self.costs = {"任务执行": [], "诊断": [], "生成（含路由）": [], "配对验证": []}
        self.round_rows = {}
        self.attempt_rounds = {}
        self.commits = 0
        self.started = False
        self.ended = False

    def block(self, title, lines=()):
        self.write("\n【" + title + "】\n" + "\n".join(str(line) for line in lines) + "\n")

    def source(self, label, ref, *, when=None):
        if ref is not None:
            if ref.uri not in self.sources:
                kind = getattr(ref, "kind", "validation")
                kind = {"controller_execution": "执行", "evolution_context": "上下文", "evolution_proposal": "生成",
                    "validation_batch": "验证批次", "local_validation": "当前任务验证", "validation_results": "独立验证",
                    "run_result": "结果", "diagnosis": "诊断"}.get(kind, kind)
                filename_time = re.search(r"\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}_\d{6}", Path(ref.uri).stem)
                timestamp = filename_time.group() if filename_time else (when or self.current_time).astimezone().strftime("%Y-%m-%d_%H-%M-%S_%f")
                self.sources[ref.uri] = f"{kind}_{timestamp}"
            path = self.readable_sources.get(ref.uri)
            if path is None:
                files = getattr(self.artifacts, "files", None)
                path = files.path(ref.uri) if files else self.root / ref.uri if self.root is not None else ref.uri
            path = str(path)
            return f"{self.sources[ref.uri]} → {path}"
        return label + "（未保存引用）"

    def begin(self, task, snapshot, config, context, *, started_at=None):
        if self.started:
            return
        self.started = True
        self.started_at = started_at or datetime.now().astimezone()
        self.current_time = self.started_at
        self.episode_id = context.episode_id
        self.version_names[snapshot.version_id] = "V0"
        validation = config.validation
        if validation.acceptance_policy == "weighted_improvement":
            weights = validation.weights
            acceptance_rule = (f"接受策略：weighted_improvement；当前任务/同类/回归权重="
                f"{weights.current_task:g}/{weights.similar:g}/{weights.regression:g}；综合收益须为正且至少 {validation.min_improvement}；"
                + ("回归硬限制关闭" if validation.max_regression_drop is None else f"回归分最大下降 {validation.max_regression_drop}"))
        else:
            acceptance_rule = f"接受阈值：独立验证分提升至少 {validation.min_improvement}；回归分最大下降 {validation.max_regression_drop}"
        self.write("\n" + "=" * 72 + "\n任务进化记录\n" + "=" * 72 + "\n")
        self.block("任务与运行条件", [f"任务：{task.task_id}；Benchmark：{task.benchmark_id}",
            f"开始：{moment(started_at)}；会话：{context.episode_id}",
            f"模式：{config.evolution.mode}；Router：{config.routing.plugin}；seed：{context.seed}",
            f"Agent：{snapshot.agent_plugin_id}；模型：{config.providers.model.model_id}",
            f"评分器：{config.assessment.evaluator}；配置：{compact(config.assessment.evaluator_config)}",
            f"工具：{', '.join(config.providers.tools.enabled) or '无'}；Agent 步骤上限：{config.agent.max_steps}",
            f"最大进化轮数：{config.limits.max_evolution_rounds}；单次输出上限：{context.max_output_tokens}",
            acceptance_rule,
            ("成功结束条件：稳定版本在当前任务上的 Evaluator.success 为 true；接受候选不等于任务成功。"
             if config.evolution.stop_on_success else
             "成功后继续进化：运行至配置轮数上限；任务成功仍由 Evaluator.success 独立记录。"),
            "总资源预算：当前 MVP 未强制执行；下文记录实际消耗。",
            "本报告展示进化决策与评测汇总；执行轨迹、输出和原始模型响应见对应详细记录。",
            "公开任务输入（完整，仅在此展示一次）：", json.dumps(task.model_dump(mode="json"), ensure_ascii=False, indent=2)])
        self.block("初始稳定版本 V0", ["初始组件内容（原始记录路径就地列出）："])
        for name, ref in snapshot.artifacts.items():
            self.component("V0", name, ref)

    def component(self, alias, name, ref, previous=None):
        label = self.source(f"{alias}/{name}", ref)
        raw = self.artifacts.get(ref)
        try:
            content = raw.decode("utf-8")
        except UnicodeError:
            self.block(f"组件 {alias}/{name}", ["原始记录：" + label, f"类型：{ref.kind}；二进制内容 {len(raw)} 字节。"])
            return
        lines = [f"类型：{ref.kind}；{'新增' if previous is None else '相对父版本更新'}"]
        if previous is not None:
            old = self.artifacts.get(previous).decode("utf-8", errors="replace")
            left, right = old.split(), content.split()
            changes = []
            for operation, a, b, c, d in difflib.SequenceMatcher(None, left, right).get_opcodes():
                if operation in {"delete", "replace"}:
                    changes.append("- " + " ".join(left[a:b]))
                if operation in {"insert", "replace"}:
                    changes.append("+ " + " ".join(right[c:d]))
            lines += ["变更片段：", excerpt("\n".join(changes) or "内容未变化", 900)]
        lines += ["组件正文（超过 16000 字符时明确节选，完整内容见本节原始记录路径）：", excerpt(content, 16000)]
        lines.insert(0, "原始记录：" + label)
        self.block(f"组件 {alias}/{name}", lines)

    def diagnosis(self, value):
        if value.record_ref:
            from aer_core.schemas.diagnosis import DiagnosisRecord
            receipt = DiagnosisRecord.model_validate_json(self.artifacts.get(value.record_ref))
            self.current_time = receipt.finished_at
        lines = [f"来源：{value.source}；标签：{value.reason_code}", f"解释：{value.explanation}",
            f"证据事件序号：{', '.join(map(str, sorted(value.all_evidence_step_ids))) or '未记录'}"]
        if value.record_ref:
            lines.insert(0, "原始诊断记录：" + self.source("诊断", value.record_ref))
        for title, items in (("观察事实", value.observations), ("可能原因（模型假设）", value.hypotheses)):
            lines.append(title + "：")
            lines.extend(f"  {item.text}；证据事件={list(item.evidence_step_ids)}" for item in items)
            if not items:
                lines.append("  未记录")
        if value.fact_check:
            lines.append("源码核对（静态推断，未执行项目测试）；证据版本：" + str(value.fact_check.get("evidence_version", "未完成")))
            claims = {claim["id"]: claim["statement"] for claim in value.fact_check.get("draft", {}).get("claims", ())}
            labels = {"supported": "源码支持", "refuted": "已反驳", "uncertain": "待核验"}
            for finding in value.fact_check.get("review", {}).get("findings", ()):
                lines.append(f"  {labels[finding['status']]}：{claims.get(finding['id'], finding['id'])}；{finding['reasoning']}")
        if value.usage:
            self.costs["诊断"].append(value.usage)
            lines.append(self.usage_line(value.usage))
        self.block("诊断器输出", lines)

    @staticmethod
    def tool_lines(execution):
        lines = []
        if execution and "selected_tools" in execution.state_summary:
            state = execution.state_summary
            lines.append("附加工具：" + (", ".join(state["selected_tools"]) or "无"))
            lines.append("工具装配：" + state["tool_setup_status"] + "；实际可用：" + (", ".join(state["available_tools"]) or "无"))
            if state.get("tool_setup_error"):
                lines.append("工具装配失败原因：" + state["tool_setup_error"])
            calls = state.get("tool_invocations", ())
            lines.append("工具调用：" + (", ".join(item["tool"] + "=" + item["status"] for item in calls[-6:]) or "未调用附加工具"))
            from aer_core.tool_usage import tool_usage_summary
            usage = tool_usage_summary(state)
            lines.append(f"客户端读取工具列表：{usage['discovered']}；不代表模型已考虑使用；省略调用明细 {usage['receipts_omitted']} 条")
            for name, counts in usage["tools"].items():
                lines.append(f"{name}：调用 {counts['attempted']}；未结束 {counts['pending']}；"
                    f"执行成功 {counts.get('execution_success', 0)}；业务成功/失败/未知 "
                    f"{counts.get('business_success', 0)}/{counts.get('business_failure', 0)}/{counts.get('business_unknown', 0)}；"
                    "输出是否被采用尚未判定")
        return lines

    def execution(self, scored, title, source):
        execution = scored.execution
        lines = [f"版本：{self.version_names.get(scored.version_id, '未命名版本')}；seed：{scored.seed}",
            f"得分：{score(scored)}；终止：{termination(scored)}；完整记录：{source}"]
        if execution and execution.state_summary.get("execution_model"):
            lines.append("执行模型：" + execution.state_summary["execution_model"])
            if execution.state_summary.get("execution_reasoning_effort") is not None:
                lines.append("推理强度：" + execution.state_summary["execution_reasoning_effort"])
        lines.extend(self.tool_lines(execution))
        if execution and "skill_library_artifact_id" in execution.state_summary:
            state = execution.state_summary
            lines.extend(["技能加载：" + state["skill_delivery"] + "；状态：" + state["skill_setup_status"],
                "可用技能：" + (", ".join(state["available_skills"]) or "无"),
                "实际读取：" + (", ".join(state["read_skills"]) or "无"),
                "执行前注入：" + (", ".join(state["injected_skills"]) or "无")])
        if scored.error:
            lines.append(f"执行/评分异常：{scored.error.code}；{scored.error.message}")
        if scored.cache_hit:
            lines.append("复用已保存结果。")
        self.block(title, lines)

    def proposal(self, value, ref):
        from aer_core.schemas.routing import JevRoutingRecord
        from .routing_report import routing_record
        attempt, context = value.attempt, value.context
        if attempt:
            self.attempt_rounds[attempt.attempt_id] = value.round_id
        label = f"第 {value.round_id} 轮生成"
        proposal_source = self.source(label, ref)
        self.write("\n" + "=" * 72 + f"\n第 {value.round_id} 轮进化\n" + "=" * 72 + "\n")
        self.block("决策依据与路由", ["本轮生成记录：" + proposal_source,
            f"起始稳定版本：{self.version_names.get(value.stable_version_id, '未命名版本')}",
            f"可行目标：{', '.join(context.feasible_targets) if context else '未记录'}",
            f"选择目标：{value.decision.target if value.decision else '未选择'}",
            f"路由理由：{value.decision.rationale if value.decision else '未记录'}",
            f"Operator：{value.operator_id or '未选择'}；生成状态：{value.response.status.value if value.response else value.status}"])
        from .optimization_report import optimization_record
        optimization = optimization_record(value, self.artifacts)
        if value.response and value.response.record_ref and value.response.record_ref.kind == "tool_construction":
            construction = json.loads(self.artifacts.get(value.response.record_ref))
            lines = [f"上游版本：{construction['upstream_revision']}；状态：{construction['status']}",
                f"模型调用：{len(construction['calls'])}；完整记录：{proposal_source} / generation.tool_construction"]
            for event in construction["events"]:
                if event["kind"] in {"plan", "replan"} and event.get("planner") == "FridayPlanner":
                    graph = event["value"]
                    lines.append(f"原生 FridayPlanner {'重新规划' if event['kind'] == 'replan' else '规划'}："
                        + (" → ".join(graph["execution_order"]) or "空计划"))
                    for name, node in graph["nodes"].items():
                        lines.append(f"  {name} ({node['type']})；前置：{', '.join(node['dependencies']) or '无'}；"
                            f"已完成：{node['completed']}")
                elif event["kind"] == "graph_blocked":
                    lines.append(f"任务图阻塞：{event['tool']}；未执行依赖它的后续节点")
                elif event["kind"] == "judgment":
                    lines.append(f"{event['tool']}：{event['status']}；通用性 {event['score']}/10；{event['reasoning']}")
                elif event["kind"] == "stored":
                    if event.get("scope") == "construction":
                        lines.append(f"构造库入库：{event['tool']}；最终交付标记：{event['deliverable']}")
                    else:
                        lines.append(f"内部入库：{event['tool']}；仍需 AER 外层配对验证")
                elif event["kind"] == "export_selected":
                    lines.append("最终交付工具选择：" + (", ".join(event["tools"]) or "无"))
                elif event["kind"] == "delivery_check":
                    lines.append(f"干净工作区交付复验：{event['tool']}；通过：{event['passed']}")
                elif event["kind"] == "export_verified":
                    lines.append("实际导出工具：" + (", ".join(event["tools"]) or "无"))
            self.block("OS-Copilot 工具构造", lines)
            feedback = value.response.construction_feedback
            if feedback and feedback.issues:
                self.block("传入后续轮次的构造反馈", [
                    f"{item.tool or '本轮'} / {item.stage} / {item.code} / {item.source} / {item.status}：{item.message}"
                    for item in feedback.issues])
        if optimization is not None:
            lines = [f"优化器：{optimization.optimizer}；版本：{optimization.engine_version}；"
                f"状态：{optimization.status}；原因：{optimization.reason}；停止原因：{optimization.stop_reason}",
                "内部搜索选择与 AER 接受、提交分开记录；下列分数为内部选择集评分。",
                f"试跑：{len(optimization.trials)} 次；反思：{len(optimization.reflections)} 次；"
                f"候选：{len(optimization.candidates)} 个；内部选择集与训练集重用：{optimization.config.get('selection_reuses_training')}",
                "完整输入、响应、候选和执行轨迹：" + proposal_source + " / generation.internal_optimization",
                "内部候选 | 父候选 | 选择集得分 | 内部选中"]
            for item in optimization.candidates:
                lines.append(f"C{item.candidate} | {', '.join('C' + str(parent) for parent in item.parents) or '起始提示词'} | "
                    f"{f'{item.score:.3f}' if item.score is not None else '未完成选择集评测'} | {item.selected}")
            if "samples_per_evaluation" in optimization.config:
                lines.append(f"每个提示词/任务固定采样 {optimization.config['samples_per_evaluation']} 次，取平均分；"
                    "同次搜索内初筛、反思与选择集排名复用相同条件下的采样结果。")
            lines.append("内部轮次 | 决策 | 候选 | 结果")
            labels = {"selected_parent": "选择父候选", "generated": "生成候选",
                      "accepted_in_search": "内部接受", "rejected_in_search": "内部拒绝", "optimizer_message": "优化器提示",
                      "sampled_evaluation": "采样汇总", "evaluation_reused": "复用评测"}
            for item in optimization.decisions:
                result = item.get("reason", "") or ("得分=" + str(item["score"]) if "score" in item else "")
                if item.get("message"):
                    result = item["message"].splitlines()[0]
                lines.append(f"{item.get('iteration', '')} | {labels.get(item['event'], item['event'])} | "
                    f"{('C' + str(item['candidate'])) if item.get('candidate') else ''} | {result}")
            lines.append("内部试跑 | 时间 | 候选 | 任务 | 得分 | 状态")
            for item in optimization.trials:
                observation = item.observation
                lines.append(f"{item.trial} | {moment(item.started_at)} | C{item.candidate} | {observation.task.task_id} | "
                    f"{f'{observation.feedback.score:.3f}' if observation.feedback else '未评测'} | "
                    f"{observation.error.code if observation.error else 'completed'}")
            lines.append("内部反思 | 时间 | tokens | 状态")
            for item in optimization.reflections:
                lines.append(f"{item.call} | {moment(item.started_at)} | {item.usage.total_tokens} | "
                    f"{item.error.code if item.error else 'completed'}")
            lines.append(self.usage_line(optimization.usage))
            self.block("Evolver 内部优化", lines)
        receipt = routing_record(value, self.artifacts)
        if receipt is not None:
            jev = isinstance(receipt, JevRoutingRecord)
            lines = [("完整决策请求与原始响应：" if jev else "完整提示词与原始响应：") + proposal_source +
                     (" / routing.decision_call" if jev else " / routing.model_call"),
                     "路由调用成本：" + self.usage_line(receipt.usage)]
            if receipt.error:
                lines.append("路由异常：" + receipt.error.message)
            self.block("Router 调用摘要", lines)
        if context:
            context_label = self.source(f"第 {value.round_id} 轮实际进化上下文", attempt.pre_state_ref, when=attempt.started_at)
            lines = [f"原始过滤上下文：{context_label}", "任务公开输入：同本报告开头；执行证据：本轮稳定版本最近一次执行。",
                "当轮允许反馈：" + compact(context.feedback),
                "当轮诊断视图：" + compact(context.diagnosis),
                "当轮历史尝试摘要（实际输入内容，长编号使用下方别名）："]
            for item in context.history:
                lines.append(f"  第 {item.round_id} 轮 / {item.task_id}；目标={item.decision.target if item.decision else '未记录'}")
                parent_name = self.version_names.get(item.stable_version_id, '历史父版本')
                after_name = self.version_names.get(item.stable_version_after,
                    parent_name if item.stable_version_after == item.stable_version_id else '历史提交版本'
                    if item.stable_version_after is not None else '未记录')
                lines.append(f"    父稳定版本={parent_name}；本轮结束后的稳定版本={after_name}；生成状态={item.attempt_status or '未记录'}")
                if item.error_code:
                    lines.append(f"    生成异常：{item.error_code}")
                for candidate in item.candidate_summaries:
                    lines.append(f"    {self.candidate_names.get(candidate.candidate_id, '历史候选')}：收益={candidate.reward}；"
                        f"验证分变化={candidate.delta_score}；同类变化={candidate.delta_similar_score}；回归变化={candidate.delta_regression_score}；"
                        f"accepted={candidate.accepted}；selected={candidate.selected}；committed={candidate.committed}；"
                        f"状态={candidate.reason}；接受规则={candidate.acceptance_reason or '未评估'}")
                    lines.extend("      " + line for line in weighted_gain_lines(candidate))
                    if candidate.actual_changes:
                        for edit in candidate.actual_changes:
                            lines.append(f"      实际修改 [{edit.component}/{edit.operation}]" + ('（已截断）' if edit.truncated else ''))
                            if edit.before:
                                lines.append(f"        删除／替换前：{edit.before}")
                            if edit.after:
                                lines.append(f"        新增／替换后：{edit.after}")
                        if candidate.changes_truncated:
                            lines.append("      实际修改已达到历史输入长度上限，存在截断或省略。")
                    elif candidate.change_summary:
                        lines.append(f"      变更摘要：{candidate.change_summary}" + ('（已截断）' if candidate.change_summary_truncated else ''))
                    if candidate.changes_unavailable:
                        lines.append("      部分组件无法读取为文本，实际修改未完整提取。")
                    if candidate.generation_rationale:
                        lines.append(f"      候选理由：{candidate.generation_rationale}" + ('（已截断）' if candidate.generation_rationale_truncated else ''))
                    if candidate.validation:
                        for field, label in (("current_task", "当前任务"), ("independent", "独立验证"),
                                             ("similar", "同类任务"), ("regression", "回归任务")):
                            observed = getattr(candidate.validation, field)
                            if observed:
                                lines.append(f"      {label}：{observed.before:.3f} → {observed.after:.3f}（{observed.delta:+.3f}）")
                    if not candidate.committed:
                        lines.append("      该候选修改未提交到稳定版本。")
            if not context.history:
                lines.append("  无")
            if value.decision:
                lines.append(f"本轮提供给 Evolver 的路由理由：{value.decision.rationale}")
            lines.append("过滤后的完整输入与执行轨迹见本节上下文路径。")
            self.block("Evolver 实际收到的反馈", lines)
        if attempt:
            self.costs["生成（含路由）"].append(attempt.usage)
            self.block("生成成本", [self.usage_line(attempt.usage)])
        if not value.response or not value.response.candidates:
            self.block("候选生成结果", ["无候选。", f"异常：{value.response.error.message if value.response and value.response.error else '无'}"])
            return
        parent = context.stable_snapshot if context else self.versions.load(value.stable_version_id)
        for index, candidate in enumerate(value.response.candidates, 1):
            alias = f"R{value.round_id}-C{index}"
            self.candidate_names[candidate.candidate_id] = alias
            self.block(f"候选 {alias}", [f"父版本：{self.version_names.get(candidate.parent_version_id, '未命名版本')}",
                f"进化目标：{candidate.target}；更新组件：{', '.join(candidate.artifact_updates)}",
                f"生成理由：{candidate.metadata.get('rationale') or '未记录'}"])
            for name, update in candidate.artifact_updates.items():
                self.component(alias, name, update, parent.artifacts.get(name))

    def validation(self, batch, ref):
        batch_label = self.source(f"第 {self.attempt_rounds.get(batch.attempt_id, '?')} 轮验证批次", ref)
        self.costs["配对验证"].append(batch.usage)
        reports = {item.candidate_id: item for item in batch.reports}
        for outcome in batch.outcomes:
            alias = self.candidate_names.get(outcome.candidate_id, "未记录候选")
            report = reports.get(outcome.candidate_id)
            if report:
                self.version_names[report.candidate_version_id] = alias
                report_source = self.source(alias + " 验证报告", outcome.validation_ref)
                pairs = []
                for name, evidence_ref in (("当前任务证据", report.local_result_ref), ("独立验证证据", report.private_results_ref)):
                    if evidence_ref:
                        label = self.source(alias + " " + name, evidence_ref)
                        evidence = ValidationEvidence.model_validate_json(self.artifacts.get(evidence_ref))
                        pairs.extend((pair, label) for pair in evidence.results)
                self.block(f"{alias} 系统配对评测（评测侧证据）", [
                    "原始验证报告：" + report_source,
                    "证据路径：" + "；".join(dict.fromkeys(label for _, label in pairs)),
                    "独立验证任务正文与原始响应未送入当轮 Evolver；下表供研究者检查。",
                    "分组 | 任务 | 旧版分 → 候选分 | 变化 | 候选终止原因 | 配对种子 | 旧版缓存 | 候选缓存"])
                for pair, _ in pairs:
                    old, new = pair.baseline, pair.candidate
                    delta = f"{new.evaluation.score - old.evaluation.score:+.3f}" if old.evaluation and new.evaluation else "未知"
                    self.write(f"{pair.group} | {old.task.task_id} | {score(old)} → {score(new)} | {delta} | {termination(new)} | {new.seed} | {'命中' if pair.baseline_cached else '未命中'} | {'命中' if pair.candidate_cached else '未命中'}\n")
                lines = [f"状态：{report.status.value}；完整批次：{batch_label}"]
                local = next((pair for pair, _ in pairs if pair.group == "local"), None)
                if local:
                    lines.extend(self.tool_lines(local.candidate.execution))
                if report.baseline_metrics and report.candidate_metrics:
                    old, new = report.baseline_metrics, report.candidate_metrics
                    lines += [f"独立验证分：{old.score:.3f} → {new.score:.3f}（不包含当前任务）",
                        f"同类得分：{old.similar_score:.3f} → {new.similar_score:.3f}；回归得分：{old.regression_score:.3f} → {new.regression_score:.3f}"]
                if report.error:
                    lines.append(f"异常：{report.error.code}；{report.error.message}")
                self.block(alias + " 验证汇总", lines)
            acceptance = outcome.acceptance
            self.block(alias + " 接受与选择", [f"通过接受规则：{acceptance.accepted if acceptance else '未评估'}；"
                f"原因：{acceptance.reason if acceptance else '未记录'}；收益：{acceptance.reward if acceptance else '未知'}",
                *weighted_gain_lines(acceptance),
                f"选中：{outcome.selected}；此时尚未提交；候选状态：{outcome.status.value}",
                f"候选约束异常：{outcome.validation_error.message if outcome.validation_error else '无'}"])
            self.round_rows[outcome.candidate_id] = (f"{alias}：{outcome.status.value}；收益={acceptance.reward if acceptance else '未知'}；"
                f"选中={outcome.selected}；实际提交=False")

    def commit(self, outcome):
        alias = self.candidate_names.get(outcome.candidate_id, "未记录候选")
        if outcome.committed:
            self.commits += 1
            stable = "V" + str(self.commits)
            self.version_names[outcome.candidate_version_id] = stable
            message = f"实际提交：成功；{alias} 升级为稳定版本 {stable}；提交后的任务结果见后文。"
        else:
            message = f"实际提交：失败；{outcome.commit_error.message if outcome.commit_error else '未记录原因'}"
        self.round_rows[outcome.candidate_id] = (f"{alias}：{outcome.status.value}；"
            f"收益={outcome.acceptance.reward if outcome.acceptance else '未知'}；选中={outcome.selected}；实际提交={outcome.committed}")
        self.block(alias + " 实际提交", [f"时间：{moment(outcome.final_recorded_at)}", message])

    @staticmethod
    def usage_line(usage):
        return (f"tokens={usage.total_tokens if usage.total_tokens is not None else '未知'}；模型调用={usage.model_calls if usage.model_calls is not None else '未知'}；"
            f"工具调用={usage.tool_calls}；耗时={usage.elapsed_seconds:.2f}s；usage={'完整' if usage.usage_complete else '不完整'}")

    def finish(self, result, ref):
        self.ended = True
        self.current_time = self.started_at + timedelta(seconds=result.usage.elapsed_seconds)
        result_source = self.source("完整运行结果", ref)
        if result.error and result.error.stage == "diagnosis":
            if result.error.usage:
                self.costs["诊断"].append(result.error.usage)
            if result.error.evidence_ref:
                self.block("诊断调用失败", [result.error.message,
                    "原始诊断记录：" + self.source("诊断异常", result.error.evidence_ref)])
        self.block("各轮候选结果", list(self.round_rows.values()) or ["未发生候选验证。"])
        self.block("最终结果", ["完整结果记录：" + result_source,
            f"任务：{result.task_id}；流程状态：{result.status.value}",
            f"进化轮数：{result.evolution_rounds_used}；实际提交数：{self.commits}",
            f"最终稳定版本：{self.version_names.get(result.final_stable_version_id, '见本节完整结果记录')}",
            f"最终评分状态：{result.final_evaluation_status}；当前任务成功：{result.final_evaluation.success if result.final_evaluation else '未评测'}",
            f"最终得分：{result.final_evaluation.score if result.final_evaluation else '未评测'}",
            f"稳定版本{'已更新' if result.initial_version_id != result.final_stable_version_id else '未更新'}",
            f"停止原因：{result.stop_reason.value}；阶段：{result.stop_stage.value}",
            f"异常：{result.error.message if result.error else '无'}"])
        lines = [name + "：" + self.usage_line(sum_usage(items)) for name, items in self.costs.items()]
        lines += ["总消耗（以 RunResult 为准）：" + self.usage_line(result.usage),
            "成本只累计实际新增调用；缓存与提交后结果复用不重复计费；阶段耗时相加不等于墙钟总耗时。"]
        self.block("成本", lines)
        self.write(f"\n[任务记录结束：{result.episode_id}]\n")

    def record(self, kind, value, ref=None):
        if kind == "controller_execution":
            timestamps = [event.timestamp for event in value.execution.trajectory if event.timestamp] if value.execution else []
            if timestamps and not value.cache_hit:
                self.current_time = max(timestamps)
            self.costs["任务执行"].append(value.charged_usage)
            count = len(self.costs["任务执行"])
            title = "初始执行" if count == 1 else f"提交后复测 {count - 1}"
            source = self.source(title, ref)
            self.execution(value, title, source)
        elif kind == "committed_task_result":
            self.block("提交后任务结果（复用候选验证）", [
                f"稳定版本：{self.version_names.get(value.version_id, '未命名版本')}；得分：{score(value)}",
                "结果来源：候选在当前任务上的执行与评分；提交后未再次执行或评分。",
                "本次新增成本：0（复用候选验证结果）。"])
        elif kind == "diagnosis":
            self.diagnosis(value)
        elif kind == "evolution_proposal":
            if value.attempt and value.attempt.finished_at:
                self.current_time = value.attempt.finished_at
            self.proposal(value, ref)
        elif kind == "validation_batch":
            if value.outcomes:
                self.current_time = max(item.final_recorded_at for item in value.outcomes)
            self.validation(value, ref)
        elif kind == "commit_outcome":
            self.commit(value)
        elif kind == "run_result":
            self.finish(value, ref)
