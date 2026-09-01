"""统一内部事件模型：所有 Provider 的调用最终都归一为 AIRequestEvent。"""
from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Any, Optional

from .sanitize import sanitize_usage_dict


def utc_now_ms() -> str:
    """UTC ISO8601 毫秒时间戳，例如 2026-08-24T08:40:09.123Z。

    内部时间统一为 UTC，避免本地时区歧义；毫秒精度由调用时刻决定，
    绝不伪造（无上游毫秒信息时仍只记录到真实采集精度）。
    """
    dt = datetime.now(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


@dataclass
class Usage:
    """Token 用量。Provider 无法提供时保持 None，不伪造数据。

    extension：Provider 返回的、无法映射到标准列的合法 usage 字段。
    必须进入 sanitized extension，绝不静默丢弃（Unknown Usage ≠ Secret）。
    """

    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None
    total_tokens: Optional[int] = None
    cache_read_tokens: Optional[int] = None
    cache_write_tokens: Optional[int] = None
    reasoning_tokens: Optional[int] = None
    extension: Optional[dict] = None  # 未知合法 usage 字段（落库前经 sanitizer 清洗）


@dataclass
class AIRequestEvent:
    provider: str
    model: Optional[str] = None
    endpoint: Optional[str] = None

    source: Optional[str] = None        # 调用来源（客户端自定义 header）
    project: Optional[str] = None       # 归属项目
    # Client：发起调用的 Agent / SDK / 工具（Codex / WorkBuddy / openai-sdk / curl …）。
    # 与 project（业务项目）、resource_id（资源）严格区分，三者不是同一概念。
    # 来源优先级：X-Monitor-Client 显式头 > 已知 User-Agent 推导 > 未归因（保持 NULL）。
    # 绝不由 provider / 模型 / API Key 猜测 client。
    client: Optional[str] = None

    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None
    total_tokens: Optional[int] = None
    reasoning_tokens: Optional[int] = None  # 推理思考 token（o1 / DeepSeek-R 等）

    # Cache（仅当 Provider 真实返回时填充；否则保持 None，不推测）
    cache_read_tokens: Optional[int] = None
    cache_write_tokens: Optional[int] = None
    cache_hit: Optional[int] = None      # 1=命中, 0=未命中, None=无数据

    latency_ms: Optional[float] = None
    status_code: Optional[int] = None

    # Cost / Billing 三轴（严格分离）
    cost: Optional[float] = None          # NULL=unknown / 0=confirmed zero / >0=charged
    currency: Optional[str] = None
    billing_status: Optional[str] = None  # paid|free|included|trial|promotional|quota|unknown
    list_cost: Optional[float] = None     # 零售参考价（可选）
    pricing_snapshot_id: Optional[str] = None  # 冻结历史价快照引用

    error: Optional[str] = None

    # 为 Agent Trace 预留（第一阶段不实现，仅透传记录）
    trace_id: Optional[str] = None
    parent_id: Optional[str] = None       # 父 Event ID（构成 span 树），客户端透传

    # Revision 2 统一字段（Step 2 schema migration 后落库；客户端不给则默认/None，不伪造）
    collector: str = "gateway"
    event_type: str = "llm_call"          # llm_call | rejected | error
    execution_id: Optional[str] = None    # 客户端上报才填，否则 None
    task_id: Optional[str] = None         # 客户端上报才填，否则 None
    resource_id: Optional[str] = None     # 消耗的 Resource（X-Monitor-Resource 显式归因）

    # 时间语义：timestamp=epoch 秒（兼容旧查询）；occurred_at=UTC ISO8601 毫秒（规范）
    occurred_at: str = field(default_factory=utc_now_ms)
    schema_version: int = field(default=2)

    # 未知合法 usage 字段，经 sanitizer 清洗后的 JSON（绝不丢失、绝不存 credential）
    usage_extension: Optional[dict] = None
    metadata: Optional[dict] = None       # 其他扩展信息（JSON 序列化落库）

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
        self.reasoning_tokens = usage.reasoning_tokens
        if usage.cache_read_tokens is None:
            self.cache_hit = None
        else:
            self.cache_hit = 1 if usage.cache_read_tokens > 0 else 0
        # 未知 usage 字段：经 sanitizer 清洗后保留，绝不静默丢弃、绝不存 credential
        if usage.extension:
            cleaned = sanitize_usage_dict(usage.extension)
            if cleaned:
                self.usage_extension = cleaned

    def to_dict(self) -> dict:
        d = asdict(self)
        # JSON 列落库前序列化
        if isinstance(d.get("usage_extension"), dict):
            d["usage_extension"] = json.dumps(d["usage_extension"], ensure_ascii=False)
        if isinstance(d.get("metadata"), dict):
            d["metadata"] = json.dumps(d["metadata"], ensure_ascii=False)
        return d
