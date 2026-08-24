"""统一内部事件模型：所有 Provider 的调用最终都归一为 AIRequestEvent。"""
from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field, asdict
from typing import Any, Optional


@dataclass
class Usage:
    """Token 用量。Provider 无法提供时保持 None，不伪造数据。"""
    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None
    total_tokens: Optional[int] = None
    cache_read_tokens: Optional[int] = None
    cache_write_tokens: Optional[int] = None


@dataclass
class AIRequestEvent:
    provider: str
    model: Optional[str] = None
    endpoint: Optional[str] = None

    source: Optional[str] = None        # 调用来源（客户端自定义 header）
    project: Optional[str] = None       # 归属项目

    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None
    total_tokens: Optional[int] = None

    # Cache（仅当 Provider 真实返回时填充；否则保持 None，不推测）
    cache_read_tokens: Optional[int] = None
    cache_write_tokens: Optional[int] = None
    cache_hit: Optional[int] = None      # 1=命中, 0=未命中, None=无数据

    latency_ms: Optional[float] = None
    status_code: Optional[int] = None

    estimated_cost: Optional[float] = None
    currency: Optional[str] = None

    error: Optional[str] = None

    # 为 Agent Trace 预留（第一阶段不实现，仅透传记录）
    trace_id: Optional[str] = None
    parent_id: Optional[str] = None       # 父 Event ID（构成 span 树），客户端透传

    # Revision 2 统一字段（Step 2 schema migration 后落库；客户端不给则默认/None，不伪造）
    collector: str = "gateway"
    event_type: str = "llm_call"
    execution_id: Optional[str] = None    # 客户端上报才填，否则 None
    task_id: Optional[str] = None         # 客户端上报才填，否则 None
    resource_id: Optional[str] = None     # 消耗的 Resource（X-Monitor-Resource 显式归因）
    metadata: Optional[dict] = None       # 扩展信息（JSON 序列化落库）

    request_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    timestamp: float = field(default_factory=time.time)

    def apply_usage(self, usage: Optional[Usage]) -> None:
        if not usage:
            return
        self.input_tokens = usage.input_tokens
        self.output_tokens = usage.output_tokens
        self.total_tokens = usage.total_tokens
        if self.total_tokens is None and (
            self.input_tokens is not None or self.output_tokens is not None
        ):
            self.total_tokens = (self.input_tokens or 0) + (self.output_tokens or 0)
        self.cache_read_tokens = usage.cache_read_tokens
        self.cache_write_tokens = usage.cache_write_tokens
        if usage.cache_read_tokens is None:
            self.cache_hit = None
        else:
            self.cache_hit = 1 if usage.cache_read_tokens > 0 else 0

    def to_dict(self) -> dict:
        d = asdict(self)
        # metadata 落库前序列化为 JSON 文本（读回时由 storage._query 反序列化）
        if isinstance(d.get("metadata"), dict):
            d["metadata"] = json.dumps(d["metadata"], ensure_ascii=False)
        return d
