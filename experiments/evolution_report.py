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
        self.seen_executions = {}
        self.costs = {"执行与复测": [], "诊断": [], "生成（含路由）": [], "配对验证": []}
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
        self.write("\n" + "=" * 72 + "\n任务进化记录\n" + "=" * 72 + "\n")
        self.block("任务与运行条件", [f"任务：{task.task_id}；Benchmark：{task.benchmark_id}",
            f"开始：{moment(started_at)}；会话：{context.episode_id}",
            f"模式：{config.evolution.mode}；Router：{config.routing.plugin}；seed：{context.seed}",
            f"Agent：{snapshot.agent_plugin_id}；模型：{config.providers.model.model_id}",
            f"工具：{', '.join(config.providers.tools.enabled) or '无'}；Agent 步骤上限：{config.agent.max_steps}",
            f"最大进化轮数：{config.limits.max_evolution_rounds}；单次输出上限：{context.max_output_tokens}",
            f"接受阈值：独立验证分提升至少 {config.validation.min_improvement}；回归分最大下降 {config.validation.max_regression_drop}",
            "总资源预算：当前 MVP 未强制执行；下文记录实际消耗。",
            "报告中的系统评测证据与当轮实际提供给 Evolver 的反馈分开列出。",
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
        if value.usage:
            self.costs["诊断"].append(value.usage)
            lines.append(self.usage_line(value.usage))
        self.block("诊断器输出", lines)

    def execution(self, scored, title, source):
        execution = scored.execution
        if execution and execution.operation_id in self.seen_executions:
            self.block(title, [f"复用已展示执行：{self.seen_executions[execution.operation_id]}；得分：{score(scored)}"])
            return
        lines = [f"版本：{self.version_names.get(scored.version_id, '未命名版本')}；seed：{scored.seed}",
            f"得分：{score(scored)}；终止：{termination(scored)}；完整记录：{source}"]
        if execution:
            lines.append(f"实际使用步数：{execution.state_summary.get('model_steps', '未记录')}；"
                         f"本次有效步数上限：{execution.state_summary.get('effective_max_steps', '未记录')}")
            self.seen_executions[execution.operation_id] = title
            events = [plain(event) for event in execution.trajectory]
            results = {event["payload"].get("call_id"): event["payload"] for event in events if event["kind"] == "tool_result"}
            model_step = 0
            for event in events:
                data, seq = event["payload"], event["seq"]
                timestamp = moment(datetime.fromisoformat(event["timestamp"].replace("Z", "+00:00"))) if event.get("timestamp") else "时间未记录"
                if event["kind"] == "model_response":
                    model_step += 1
                    if data.get("tool_calls"):
                        action = "请求工具 " + ", ".join(call["name"] for call in data["tool_calls"])
                    else:
                        action = "返回最终响应" if execution.output else "返回文本响应"
                    lines.append(f"  {timestamp} | 模型步骤 {model_step} | 事件 {seq} | {action}；finish_reason={data.get('finish_reason', '未记录')}")
                    if data.get("content") and (not data.get("tool_calls") or str(data.get("finish_reason")) == "length"):
                        lines.append("    响应：" + excerpt(data["content"], 700))
                elif event["kind"] == "tool_call":
                    result = results.get(data.get("call_id"))
                    returned = compact(result.get("output", {})) if result else "未记录返回"
                    if result and result.get("error"):
                        returned = f"{result['error']['code']}：{result['error']['message']}"
                    lines.append(f"  事件 {seq} | {data.get('name')} {compact(data.get('arguments', {}))} → {excerpt(returned, 500)}")
                elif event["kind"] in {"step_limit", "output_parse_error", "execution_error"}:
                    lines.append(f"  事件 {seq} | {event['kind']} | {excerpt(compact(data), 500)}")
            lines += ["最终解析输出：", json.dumps(execution.model_dump(mode="json")["output"], ensure_ascii=False, indent=2)]
        if scored.error:
            lines.append(f"执行/评分异常：{scored.error.code}；{scored.error.message}")
        lines.append(self.usage_line(scored.usage))
        self.block(title, lines)

    def proposal(self, value, ref):
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
        receipt = routing_record(value, self.artifacts)
        if receipt is not None:
            lines = []
            for message in receipt.request.messages:
                lines += [f"[{message.role}]", message.content or ""]
            lines += ["模型原始响应：", receipt.response.content or "[无文本响应]" if receipt.response else "[调用失败，无响应]",
                      "路由调用成本：" + self.usage_line(receipt.usage)]
            if receipt.error:
                lines.append("路由异常：" + receipt.error.message)
            self.block("Router 实际收到的提示词与返回结果（完整正文）", lines)
        if context:
            context_label = self.source(f"第 {value.round_id} 轮实际进化上下文", attempt.pre_state_ref, when=attempt.started_at)
            lines = [f"原始过滤上下文：{context_label}", "任务公开输入：同本报告开头；执行证据：本轮稳定版本最近一次执行。",
                "当轮允许反馈：" + compact(context.feedback),
                "当轮诊断视图：" + compact(context.diagnosis),
                "当轮历史尝试摘要（实际输入内容，长编号使用下方别名）："]
            for item in context.history:
                lines.append(f"  第 {item.round_id} 轮 / {item.task_id}；目标={item.decision.target if item.decision else '未记录'}")
                for candidate in item.candidate_summaries:
                    lines.append(f"    {self.candidate_names.get(candidate.candidate_id, '历史候选')}：收益={candidate.reward}；"
                        f"验证分变化={candidate.delta_score}；同类变化={candidate.delta_similar_score}；回归变化={candidate.delta_regression_score}；"
                        f"accepted={candidate.accepted}；selected={candidate.selected}；committed={candidate.committed}；原因={candidate.reason}")
            if not context.history:
                lines.append("  无")
            lines.append("其余轨迹正文见已展示执行；过滤后的完整输入见本节上下文路径，未把私有验证轨迹补入此视图。")
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
                    "分组 | 任务 | 旧版分 → 候选分 | 变化 | 候选终止原因 | 配对种子 | 旧版缓存"])
                for pair, _ in pairs:
                    old, new = pair.baseline, pair.candidate
                    delta = f"{new.evaluation.score - old.evaluation.score:+.3f}" if old.evaluation and new.evaluation else "未知"
                    self.write(f"{pair.group} | {old.task.task_id} | {score(old)} → {score(new)} | {delta} | {termination(new)} | {new.seed} | {'命中' if pair.baseline_cached else '未命中'}\n")
                for pair, label in pairs:
                    if pair.group == "local":
                        self.execution(pair.baseline, f"{alias} 当前任务：旧版执行", label + " / baseline")
                        self.execution(pair.candidate, f"{alias} 当前任务：候选执行", label + " / candidate")
                    elif (not pair.baseline.complete or not pair.candidate.complete or
                          not pair.candidate.evaluation.success or
                          pair.baseline.evaluation.score != pair.candidate.evaluation.score):
                        self.failure_evidence(pair, alias, label)
                lines = [f"状态：{report.status.value}；完整批次：{batch_label}"]
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
                f"选中：{outcome.selected}；此时尚未提交；候选状态：{outcome.status.value}",
                f"候选约束异常：{outcome.validation_error.message if outcome.validation_error else '无'}"])
            self.round_rows[outcome.candidate_id] = (f"{alias}：{outcome.status.value}；收益={acceptance.reward if acceptance else '未知'}；"
                f"选中={outcome.selected}；实际提交=False")

    def failure_evidence(self, pair, alias, label):
        lines = [f"旧版：{score(pair.baseline)} / {termination(pair.baseline)}；候选：{score(pair.candidate)} / {termination(pair.candidate)}"]
        for side, scored in (("旧版", pair.baseline), ("候选", pair.candidate)):
            execution = scored.execution
            lines.append(side + "输出：" + (excerpt(compact(execution.model_dump(mode="json")["output"]), 500) if execution else "无"))
            if execution:
                responses = [plain(event) for event in execution.trajectory if event.kind == "model_response"]
                if responses and (not scored.evaluation or not scored.evaluation.success):
                    last = responses[-1]
                    lines.append(f"{side}末次响应事件 {last['seq']}；finish_reason={last['payload'].get('finish_reason')}：" + excerpt(last["payload"].get("content") or "[工具调用，尚无文本输出]", 500))
                for event in execution.trajectory:
                    if event.kind == "tool_result" and event.payload.get("error"):
                        data = plain(event)["payload"]["error"]
                        lines.append(f"{side}工具异常事件 {event.seq}：{data.get('code')}；{excerpt(data.get('message'), 220)}")
            if scored.error:
                lines.append(f"{side}异常：{scored.error.code}；{scored.error.message}")
        lines.append("完整执行轨迹：" + label + " / " + pair.baseline.task.task_id)
        self.block(f"{alias} 变化或失败证据：{pair.baseline.task.task_id}", lines)

    def commit(self, outcome):
        alias = self.candidate_names.get(outcome.candidate_id, "未记录候选")
        if outcome.committed:
            self.commits += 1
            stable = "V" + str(self.commits)
            self.version_names[outcome.candidate_version_id] = stable
            message = f"实际提交：成功；{alias} 升级为稳定版本 {stable}；提交后执行见后续复测。"
        else:
            message = f"实际提交：失败；{outcome.commit_error.message if outcome.commit_error else '未记录原因'}"
        self.round_rows[outcome.candidate_id] = (f"{alias}：{outcome.status.value}；"
            f"收益={outcome.acceptance.reward if outcome.acceptance else '未知'}；选中={outcome.selected}；实际提交={outcome.committed}")
        self.block(alias + " 实际提交", [f"时间：{moment(outcome.final_recorded_at)}", message])

    @staticmethod
    def usage_line(usage):
        return (f"tokens={usage.total_tokens if usage.total_tokens is not None else '未知'}；模型调用={usage.model_calls}；"
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
            "最终输出：" + (compact(result.final_execution.model_dump(mode="json")["output"]) if result.final_execution else "无本版本的有效评分输出"),
            f"稳定版本{'已更新' if result.initial_version_id != result.final_stable_version_id else '未更新'}",
            f"停止原因：{result.stop_reason.value}；阶段：{result.stop_stage.value}",
            f"异常：{result.error.message if result.error else '无'}"])
        lines = [name + "：" + self.usage_line(sum_usage(items)) for name, items in self.costs.items()]
        lines += ["总消耗（以 RunResult 为准）：" + self.usage_line(result.usage),
            "配对验证成本只累计实际调用；旧版缓存不重复计费；阶段耗时相加不等于墙钟总耗时。"]
        self.block("成本", lines)
        self.write(f"\n[任务记录结束：{result.episode_id}]\n")

    def record(self, kind, value, ref=None):
        if kind == "controller_execution":
            timestamps = [event.timestamp for event in value.execution.trajectory if event.timestamp] if value.execution else []
            if timestamps:
                self.current_time = max(timestamps)
            self.costs["执行与复测"].append(value.usage)
            count = len(self.costs["执行与复测"])
            title = "初始执行" if count == 1 else f"提交后复测 {count - 1}"
            source = self.source(title, ref)
            self.execution(value, title, source)
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
