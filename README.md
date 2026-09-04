# AI Resource Monitor

轻量、免费、本地优先的 AI 调用资源监控。第一阶段：**多 Provider 统一监控底座**。

```
AI App ──→ Monitor Gateway ──→ Provider Router ──→ Adapter ──→ 真实 Provider
                │                                        │
                └──── 统一事件 AIRequestEvent ──→ SQLite ──→ Dashboard
```

Provider 协议差异全部隔离在 Adapter 层，Monitor Core 不含任何 Provider 硬编码。

## 支持的 Provider

| Provider | 协议 | 说明 |
|---|---|---|
| OpenAI | OpenAI-compatible | |
| DeepSeek | OpenAI-compatible | 复用通用适配器 |
| Kimi | OpenAI-compatible | 复用通用适配器 |
| MiniMax | OpenAI-compatible | 复用通用适配器 |
| Gemini | Native | 独立 Adapter（generateContent 协议） |
| OpenRouter / Groq / 智谱 / 通义 / 豆包 | OpenAI-compatible | 已预注册，配置即可用 |

新增 Provider 三步：新增/复用 Adapter → `registry.py` 注册一行 → `pricing_data.yaml` 加价格。Monitor Core 零改动。

## 快速开始

```bash
# 方式一：可编辑安装（推荐，提供 `arm` 命令）
pip install -e ".[dev]"
arm            # 启动 Monitor 并自动打开浏览器（Dashboard: http://127.0.0.1:8787/）

# 方式二：仅装运行时依赖
pip install -r requirements.txt
python scripts/start.py          # 启动 Monitor 并自动打开浏览器
# Windows 也可双击 start-monitor.bat；macOS / Linux 运行 ./start-monitor.sh
```

启动器会检测端口占用（已运行时只打开浏览器、不重复启动）、等待健康检查就绪后再打开页面。
打开 Dashboard 即可看到**已有历史用量**（Requests / Input·Output Tokens / Cache / Cost / Errors），
无需先发起新请求。

1. 打开 Dashboard，在「Provider 配置」中填入 API Key 并启用
2. 把 AI 应用的 base_url 指向网关（SDK 代码零改动）：

```python
# DeepSeek 示例
client = OpenAI(
    base_url="http://127.0.0.1:8787/gateway/deepseek",   # 原: https://api.deepseek.com
    api_key="任意值",   # 真实 key 由 Monitor 侧持有
)
```

Gemini 应用把 `https://generativelanguage.googleapis.com` 换成
`http://127.0.0.1:8787/gateway/gemini` 即可。

### Resource 与归因

Monitor 把"你实际拥有的 AI 资源"称为 **Resource**（一次付费额度、一个免费账号、
一份订阅等）。一个 Provider 可以定义**多个** Resource（例如 DeepSeek 的「付费」与
「免费」额度），在 `data/config.yaml` 的 `resources:` 段定义，模板见 `config.example.yaml`。

请求如何归属到 Resource（确定性、可解释，绝不猜测）：

1. **显式** `X-Monitor-Resource: <resource_id>` 请求头 → 权威归因
   （未注册 / 已禁用的 resource_id 会被拒绝，不会静默错归）。
2. Provider 配置了 `default_resource_id` → 该 Provider 无头流量默认归入它。
3. 该 Provider 恰好只有**一个** enabled Resource → 确定性归入它。
4. 以上皆不满足 → 记为 **未归因（unattributed）**，绝不按 Provider 名硬猜。

每一笔事件都会保留 `attribution_source`（explicit_header / provider_default /
unique_resource / unattributed），可在 `/api/attribution/coverage` 查看整体覆盖度，
搞清楚"为什么我的 Resource 数据这么少"。

### Project 归因与 Client（Agent）归因

除了 Resource，Monitor 还会记录**这次调用属于哪个 Project、来自哪个 Client（Agent）**：

- **Project**（P6-D）：请求级 `X-Monitor-Project: <name>` 头优先级最高；未带时，若该
  Provider 配置了 `default_project`（见 `config.example.yaml`），则归入该 Project。都未指定
  → `project = NULL`（显示为 `Unknown`），绝不按 Provider / 模型猜测。
- **Client / Agent**（P6-E）：标识调用方是谁（Codex / WorkBuddy / 你的脚本 / curl 等）。
  - 解析顺序：`X-Monitor-Client` 头 → `User-Agent` → 无法识别则记为该 `User-Agent` 字面量
    （例如 `curl`、`openai-sdk`，**不猜测为某个 Agent**）。
  - 未提供任何线索 → `client = NULL`（显示 `Unknown`）。
  - 例：Codex 调用时带 `X-Monitor-Client: codex`；OpenAI SDK 默认会被识别为 `openai-sdk`。
- 每笔事件在 `metadata` 中保留三个来源字段：`attribution_source` /
  `project_attribution_source` / `client_attribution_source`，可在请求详情中查看，做到
  **归因可解释、不黑盒**。

### 使用量、缓存与成本

- **Usage 统计**：每笔请求记录 Input / Output / Total tokens，Provider 未返回时记 `null`，
  不伪造。
- **Cache**（P6-B）：兼容 OpenAI 的 `prompt_tokens_details.cached_tokens`、
  `prompt_cache_hit_tokens`、`cached_tokens`，以及 Gemini 的
  `usageMetadata.cachedContentTokenCount`。区分 **cache read**（命中复用的 token）与
  **cache write**（新写入的 token）；Provider 不提供字段时保持 `null`，**绝不当成 0**。
  - Dashboard 的 **CACHE HIT** 卡片展示 `cache_hit_rate`（命中请求 / 可观测请求）与
    `cache_coverage`（可观测请求 / 总请求），二者口径不同，请勿混读。
- **成本按每百万 token（per-1M）计**（P6-C）：价格来自 `monitor/pricing_data.yaml`
  （`unit: per_1m_tokens`），成本在写入事件时即按当时价格冻结，后续调价不回溯改历史。
- **跨币种**：成本按币种分组（`cost_by_currency`）。同一 Resource 出现多币种时**绝不给出
  单一汇总金额**（无汇率、不换算），Dashboard 会逐币种列出并标记 `MULTI-CURRENCY`。
  `unknown`（有用量但拿不到价）与 `unattributed`（无 Resource 标）**都显示 N/A / UNKNOWN
  徽标，绝不显示 ¥0**。

### 自定义 OpenAI 兼容 API（P6-F）

任何暴露 `/v1/chat/completions` 的 OpenAI 兼容服务（自建 vLLM / Ollama / llama.cpp /
内网网关 / 第三方兼容层）都可直接接入，**无需新增代码**：

```yaml
# data/config.yaml
providers:
  private-llm:
    enabled: true
    base_url: http://localhost:8000   # 你的服务
    api_key: ""
    default_project: local-experiments
```

凭据通过环境变量 `PRIVATE_LLM_API_KEY` 或 Dashboard 凭据面板提供（不写进 `config.yaml`）。
把 AI 应用的 `base_url` 指向 `http://127.0.0.1:8787/gateway/private-llm/v1/...` 即可，
Gateway 自动复用 OpenAI 兼容适配器路由（不另起第二套代理）。

> 经 Monitor 代理后，Codex / WorkBuddy / 你的所有脚本的 AI 调用都会自动获得
> Resource / Project / Client 三层归因与统一的 Usage / Cost 看板。

3. Dashboard 统一查看 Requests / Tokens / Cost / Latency / Error Rate

## 验证

```bash
pytest tests/                 # 单元测试 + 本地 mock 端到端链路测试
python scripts/verify.py      # 真实 Provider 链路验证（需先配置 key）
```

## 架构要点

- **统一事件模型** `AIRequestEvent`：provider/model/tokens/cost/latency/error + 预留 `trace_id`/`parent_span_id`（为后续 Agent Trace 留位）
- **Usage 缺失不伪造**：Provider 未返回 token 时记 `null`
- **PricingRegistry**：价格唯一来源在 `monitor/pricing_data.yaml`，支持版本化模型名前缀回退（`gpt-4o-2024-08-06` → `gpt-4o`）
- **API Key 安全**：绝不写入 `data/config.yaml`（写入前会被剔除、加载时会被丢弃）；仅存环境变量（`UPPER_PROVIDER_API_KEY`，优先级最高）或本机凭据文件 `data/credentials.json`（0600）；不落事件库、不进日志、不下发前端、不在事件模型中
- **可选 header**：`x-monitor-source` / `x-monitor-project`（`X-Monitor-Project`）/
  `x-monitor-client`（`X-Monitor-Client`）/ `x-monitor-resource`（`X-Monitor-Resource`）/
  `x-trace-id` / `x-parent-span-id`

## 目录

```
monitor/
  main.py            # FastAPI 入口 + 配置/统计 API
  gateway.py         # 统一网关（转发/流式/计时/落库）
  registry.py        # ProviderRegistry
  pricing.py         # PricingRegistry
  pricing_data.yaml  # 价格数据（唯一价格来源）
  events.py          # AIRequestEvent
  storage.py         # SQLite
  config.py          # Provider 配置管理
  providers/         # Adapter 层（base / openai_compat / gemini）
dashboard/           # 本地静态 Dashboard
scripts/verify.py    # 真实链路验证
tests/               # 单元 + 端到端（mock 上游）
data/                # config.yaml + monitor.db（本地数据）
```

## 第一阶段不实现（已预留架构）

VS Code 插件 / SDK / OpenTelemetry / Agent Trace / 自动路由 / 成本优化 / 云端同步。
