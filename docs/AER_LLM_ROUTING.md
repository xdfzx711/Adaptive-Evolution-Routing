# 模型路由

`llm_router` 通过注入的 ModelProvider，根据公开任务、执行轨迹、评分反馈、诊断、历史尝试和组件内容选择一个可行目标。目标操作说明由 Evolver 的 capabilities 提供；Router 不生成工件、不决定接受或提交。

系统提示词位于 `plugins/routers/llm.py`。用户消息包含 operation、过滤后的 context 和 stable_components，输出为 `{"target":"model","rationale":"根据公开证据选择组件"}`。非法 JSON、越界目标、工具调用、截断或超时产生路由错误，实际请求和响应仍写入当轮日志。

当前 Codex 配置支持 prompt/model，Qwen ReAct 配置支持 prompt。步数 Evolver 已移除。Codex 接入、模型候选列表、命令行参数、版本与成本语义见 [Codex CLI Agent](AER_CODEX_AGENT.md)。
