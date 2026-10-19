# Qwen 模型接入

qwen_model 实现与 scripted_model 相同的 ModelProvider 接口，ReAct Agent 和 Single-call Agent 都可通过 YAML 切换。实现使用 HTTPX 直接调用 [Qwen 的 Chat Completions 兼容 API](https://www.alibabacloud.com/help/zh/model-studio/qwen-api-via-openai-chat-completions)，同步、非流式、无自动重试。

## 配置 .env

复制项目根目录的 .env.example 为 .env，填入 API key、兼容接口 URL 和模型名：

```dotenv
QWEN_API_KEY=你的API_KEY
QWEN_BASE_URL=你的兼容接口BASE_URL
QWEN_MODEL=qwen-plus
```

BASE_URL 可以是以 /v1 结尾的兼容 API 基础地址，也可以是完整 /chat/completions 地址。基础地址会追加 /chat/completions；完整地址直接使用。原生 DashScope generation 地址不是本 Provider 的接口。地域或业务空间地址以你的服务控制台为准。

.env 已加入 gitignore，.env.example 保留在项目中。密钥仅作为运行时 SecretStr 注入 Provider，不进入 YAML、Snapshot、模型请求轨迹或 manifest。HTTP 错误保留状态码，不保存可能回显请求头的原始服务错误正文。

dotenv_path 相对 YAML 所在目录解析，示例 ../../.env 指向项目根目录。加载不依赖当前工作目录，不修改进程环境；已存在的进程环境变量优先于文件。文件值不做变量插值。若现有 .env 使用其他变量名，可以修改 api_key_env、base_url_env。

validate-config 和 inspect-benchmark 不需要密钥；存在 QWEN_MODEL 时会解析实际模型名，缺失时允许保留变量引用用于静态检查。实际装配 Qwen、初始化其 Snapshot 或 execute-task 时要求解析出模型名；缺失时在创建执行运行目录前报告清楚的配置错误。

## 执行两个 Agent

```powershell
.\.venv\Scripts\aer.exe execute-task experiments/configs/v0_1_qwen_react.yaml --task-id dev-001
.\.venv\Scripts\aer.exe execute-task experiments/configs/v0_1_qwen_single_call.yaml --task-id dev-001
```

模型名在 .env 的 QWEN_MODEL 中设置，填入你的 API 支持的具体型号。YAML 使用 "${QWEN_MODEL}" 引用；启动时将实际名称写入解析配置、Snapshot 与 manifest，API 请求使用该名称。URL 和模型配置变更后使用新的 run-id，以保持已保存运行的来源一致。

Provider 配置示例：

```yaml
providers:
  model:
    plugin: qwen_model
    model_id: "${QWEN_MODEL}"
    config:
      dotenv_path: ../../.env
      api_key_env: QWEN_API_KEY
      base_url_env: QWEN_BASE_URL
      temperature: 0.0
      enable_thinking: false
      send_seed: false
      response_format: null
```

enable_thinking 是直接 HTTP 请求的顶层参数。设为 null 时不发送，用于不接受此参数的兼容服务。模型的非流式、工具与思考模式能力以实际接口为准。Provider 只保留可见 content 与工具调用，不记录 reasoning_content。

send_seed 为 true 时发送当前请求 seed，取值须不超过 2^31-1；false 时不发送。固定 seed 或 temperature=0 都不能保证远程模型输出完全一致。response_format 可为 null（不发送）或 json_object；使用 json_object 时提示词须明确要求 JSON，且模型必须支持该功能。参见 [官方接口参数](https://www.alibabacloud.com/help/zh/model-studio/qwen-api-via-openai-chat-completions)。

## 工具、成本与超时

Provider 将 AER 工具 schema 转成 function tools，将 assistant 工具调用和 tool 观察转换成兼容消息格式。模型返回的 JSON arguments 转为标准 ToolCall；不可解析的响应保留已知 usage 并报告 contract_error。

ReAct 提供 calculator 并允许模型自行决定是否调用，Single-call 不提供工具。两者使用相同公开任务输入和现有确定性 Evaluator；不会将正确答案或评分规格发给 API。

UsageReport 使用服务返回的 prompt_tokens 与 completion_tokens，不估算缺失值，不额外累加 cached_tokens 或 reasoning_tokens。细分 token 信息与返回的模型/响应 ID 放在 ModelResponse.metadata 中。失败请求若没有 usage，token 为 null，来源为 unknown，保留一次请求尝试。每个 HTTP 调用只有一次派发，无隐式重试。

HTTPX 的连接、读写和连接池超时来自 model_call_timeout_seconds；这些是网络阶段超时，并非 episode 总时间硬预算。Agent 在返回后额外检查整次调用耗时。HTTPX 超时抛出 OperationTimeout。HTTPX 的超时语义见 [官方文档](https://www.python-httpx.org/advanced/timeouts/)。

execution_manifest.json 另保存实际端点、模型名、参数、seed 发送策略和重试策略；密钥不会写入文件。返回记录与脚本模型相同，usage_source 反映真实报告或缺失信息。

## 验证范围

本地测试使用 MockTransport 覆盖最终响应、工具往返、错误、超时、未知 usage、环境优先级及密钥隔离。真实 API 连接、账号模型权限与具体型号表现需要填入 .env 后联调。当前接入范围为文本 Chat Completions，不包含流式、多模态或原生 generation API。
