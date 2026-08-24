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
pip install -r requirements.txt
python -m monitor.main          # 启动，Dashboard: http://127.0.0.1:8787/
```

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
- **API Key 安全**：仅存本地 `data/config.yaml`；不落库、不进日志、不下发前端、不在事件模型中
- **可选 header**：`x-monitor-source` / `x-monitor-project` / `x-trace-id` / `x-parent-span-id`

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
