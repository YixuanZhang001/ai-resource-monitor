"""OpenRouter Observation Collector — 首个真实 Provider Collector（P0-8）。

官方 API 契约（2026-08 确认，非猜测）：
    GET https://openrouter.ai/api/v1/credits
    Authorization: Bearer <management/provisioning API key>   （仅管理 key 可查）
    无 request body / query
    200 → {"data": {"total_credits": 100.5, "total_usage": 25.75}}
    401 invalid/missing auth | 403 非管理 key | 429/500/502/503/504 可重试
    无 quota / reset / expiry 语义 → quota/reset_at/expires_at = NULL

映射：
    balance   = total_credits（Provider 原始字段：已购买总 credits，USD 计价）
    quota     = NULL（无 quota 概念）
    remaining = total_credits - total_usage（推导值，metadata 标注 remaining_source=derived；
                只有 total_usage 存在才计算，否则 NULL）
    source    = "api"
    失败(401/403/timeout/网络/解析) → status=error（消息经 sanitize，绝不泄露 key）

Secret 安全：key 只经 CredentialProvider 获取，绝不进入 Observation/DB/日志/错误消息。
"""
from __future__ import annotations

from typing import Optional

import httpx

from ..credential import CredentialProvider
from ..observe import ObservationCollector, ResourceObservation
from ..resource import ResourceDefinition
from ..sanitize import sanitize_error

CREDITS_URL = "https://openrouter.ai/api/v1/credits"


class OpenRouterObservationCollector(ObservationCollector):
    def __init__(self, credential_provider: Optional[CredentialProvider] = None,
                 client: Optional[httpx.Client] = None,
                 timeout: float = 10.0):
        self._cred = credential_provider or CredentialProvider()
        self._client = client      # 测试可注入 MockTransport client；None 则每次新建
        self._timeout = timeout

    def observe(self, resource: ResourceDefinition) -> ResourceObservation:
        # 1) 校验（Provider-specific 边界：只存在于 Collector，不进 Core）
        if resource.provider != "openrouter":
            raise ValueError(
                f"OpenRouterObservationCollector 不适用于 provider={resource.provider!r}")
        if not resource.enabled:
            return ResourceObservation(
                resource_id=resource.resource_id, status="unavailable",
                source="api", error="resource disabled")

        # 2) credential（引用 → 真实 secret，仅此处持有）
        key = self._cred.get(resource.credential_id or "openrouter")
        if not key:
            return ResourceObservation(
                resource_id=resource.resource_id, status="error",
                source="api", error="credential unavailable")

        # 3) 调用官方 API（有限 timeout，不阻塞 Monitor）
        try:
            client = self._client or httpx.Client(timeout=self._timeout)
            try:
                resp = client.get(
                    CREDITS_URL,
                    headers={"Authorization": f"Bearer {key}",
                             "Accept": "application/json"})
            finally:
                if self._client is None:
                    client.close()
        except httpx.HTTPError as e:
            return ResourceObservation(
                resource_id=resource.resource_id, status="error",
                source="api",
                error=sanitize_error(f"openrouter credits 请求失败: {type(e).__name__}"))

        # 4) 状态码处理（401/403 等）
        if resp.status_code != 200:
            return ResourceObservation(
                resource_id=resource.resource_id, status="error",
                source="api",
                error=sanitize_error(
                    f"openrouter credits HTTP {resp.status_code}"))

        # 5) 解析（malformed JSON → error）
        try:
            data = resp.json()
            d = (data or {}).get("data") or {}
            total_credits = d.get("total_credits")
            total_usage = d.get("total_usage")
        except (ValueError, TypeError):
            return ResourceObservation(
                resource_id=resource.resource_id, status="error",
                source="api", error="openrouter credits 响应解析失败")
        if total_credits is None:
            return ResourceObservation(
                resource_id=resource.resource_id, status="error",
                source="api", error="openrouter credits 缺少 total_credits")

        # 6) remaining 推导（来源明确标注，不伪装成 Provider 原始字段）
        remaining = None
        if total_usage is not None:
            remaining = round(float(total_credits) - float(total_usage), 6)
        metadata = {
            "provider": "openrouter",
            "endpoint": "/api/v1/credits",
            "total_usage": total_usage,
            "remaining_source": "derived" if remaining is not None else None,
        }
        return ResourceObservation(
            resource_id=resource.resource_id, status="known",
            balance=float(total_credits), quota=None, remaining=remaining,
            source="api", metadata=metadata)
