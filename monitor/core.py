"""Monitor Core — Revision 2。

职责：
  Collector raw observation
      ↓  ingest(raw)
   normalize → AIRequestEvent（填 collector/event_type；execution_id/task_id 不伪造）
   pricing   → cost（Step 4 从 Gateway 迁入；Core 不知具体 Provider）
   persist   → Storage
      ↓
   return event identifier (request_id)

Core 不依赖：FastAPI / httpx / SSE / 网络。
Provider-specific usage 提取在 Adapter 层（extract_usage/extract_cache_usage），
Core 只接收已归一化的 token 数值。
"""
from __future__ import annotations

import json
from typing import Optional

from .events import AIRequestEvent, Usage
from .pricing import PricingRegistry
from .storage import EventStore

# 构造 AIRequestEvent 时从 raw 提取的直通字段
_DIRECT_FIELDS = (
    "provider", "model", "endpoint", "source", "project",
    "input_tokens", "output_tokens", "total_tokens",
    "cache_read_tokens", "cache_write_tokens", "cache_hit",
    "latency_ms", "status_code",
    "cost", "currency", "billing_status", "list_cost", "pricing_snapshot_id", "error",
    "trace_id", "parent_id",
    "collector", "event_type", "execution_id", "task_id", "resource_id",
    "metadata",
)


class MonitorCore:
    """ingest → normalize → pricing → persist → 返回 event_id。"""

    def __init__(self, store: EventStore, pricing: Optional[PricingRegistry] = None):
        self.store = store
        self.pricing = pricing

    def normalize(self, raw: dict) -> AIRequestEvent:
        """Collector 原始数据 → 内部统一事件模型。

        - collector 默认 'gateway'，event_type 默认 'llm_call'
        - execution_id / task_id 客户端不给则 None（绝不伪造）
        - request_id 客户端不给则由 AIRequestEvent 默认生成
        """
        kw = {f: raw[f] for f in _DIRECT_FIELDS if raw.get(f) is not None}
        kw.setdefault("collector", "gateway")
        kw.setdefault("event_type", "llm_call")
        # metadata 兼容：Gateway 经 to_dict 传入的可能是 JSON text
        m = kw.get("metadata")
        if isinstance(m, str):
            try:
                kw["metadata"] = json.loads(m)
            except (ValueError, TypeError):
                pass
        if raw.get("request_id"):
            kw["request_id"] = raw["request_id"]
        if raw.get("timestamp"):
            kw["timestamp"] = raw["timestamp"]
        return AIRequestEvent(**kw)

    def ingest(self, raw: dict) -> str:
        """Collector 提交原始观测 → normalize → pricing → persist → event_id。

        pricing 由 Core 承担（Step 4 从 Gateway 迁入）。
        Gateway 不再决定最终成本。
        """
        event = self.normalize(raw)
        if self.pricing:
            usage = Usage(
                input_tokens=event.input_tokens,
                output_tokens=event.output_tokens,
                total_tokens=event.total_tokens,
                cache_read_tokens=event.cache_read_tokens,
                cache_write_tokens=event.cache_write_tokens,
            )
            cost = self.pricing.compute_cost(event.provider, event.model, usage)
            if cost:
                event.cost = cost.amount
                event.currency = cost.currency
        self.store.insert(event)
        return event.request_id
