# Jev Router 接入

`jev_router` 是独立的 Router 插件。它读取经过反馈权限过滤的进化上下文，只在 `feasible_targets` 中选择一个目标。Codex CLI 仍按 Snapshot 选择执行模型；Diagnoser 与 Evolver 仍使用原有的 Qwen Provider。主控制器不区分 Jev 和其他 Router。

在项目根目录的 `.env` 中填写：

```dotenv
JEV_API_KEY=你的密钥
JEV_BASE_URL=https://api.typesafe.ai
JEV_MODEL=jev-latest
```

变量名和 `.env` 路径由 [Jev 示例配置](../experiments/configs/v0_1_codex_swebench_jev.yaml) 的 `routing.config` 指定。进程环境变量优先于 `.env`。`JEV_BASE_URL` 可填 API 根地址、`/v1` 地址或完整的 `/v1/systemone` 地址；密钥不会写入配置、分轮报告或请求记录。`validate-config` 只校验配置结构；启动并装配 Router 时才检查这些环境变量。

填好密钥后，可以运行一个任务：

```powershell
.\.venv\Scripts\python.exe main.py run-task experiments/configs/v0_1_codex_swebench_jev.yaml --task-id psf__requests-1963 --output jev-result.json
```

本配置保留三项 SWE-bench 主任务、各自的同类和回归验证任务，最多进化五轮。要切回 Qwen Router，可在 YAML 中将 `routing.plugin` 改为 `llm_router` 并删除 Jev 专用的 `routing.config`。目前 `evolution.mode: llm` 是框架既有的动态路由模式名称，Jev 并未走 Qwen 的聊天接口。

当可行目标有两个或更多时，插件向 Jev 的 `POST /v1/systemone` 发送结构化 `state` 和一个 `Choice` 问题。选项由当前可行目标产生，包含各目标的插件描述。请求状态包含公开任务、当前稳定组件摘要、最近执行轨迹、允许反馈、诊断和历史进化结果；长度由 `routing.config` 限制。Jev 的 `choice` 必须属于当前可行目标，概率和置信度必须有效。只有一个可行目标时直接选择它，不消耗 Jev 调用。

查看 `runs/<运行时间>/evolution.txt` 可以看到每轮的目标与调用记录位置。对应的 `rounds/<任务-时间>/round-NNN/record.json` 中，`routing.decision_call` 保留实际请求状态、原始回答、usage 和错误；密钥不在其中。Jev 返回的是选择、概率和置信度，[不会生成文字解释](https://docs.typesafe.ai/concepts/system-one)，因此报告中的路由理由只是程序对这些数字的摘要，不应当解读为模型推理过程。请求格式按 [TypeSafe API 文档](https://docs.typesafe.ai/introduction/quickstart) 实现。
