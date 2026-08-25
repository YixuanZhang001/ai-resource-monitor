"""DeepSeek Observation Collector — Provider-Agnostic Observation 抽象实现（Phase 2C）。

官方 API 契约（2026-08 官方文档确认，非猜测）：
    GET https://api.deepseek.com/user/balance
    Authorization: Bearer <DEEPSEEK_API_KEY>
    无 request body / query
    200 → {"is_available": bool,
           "balance_infos": [{"currency": "CNY"|"USD",
                              "total_balance": "110.00",
                              "granted_balance": "10.00",
                              "topped_up_balance": "100.00"}]}
    401 鉴权失败 | 429 限流 | 5xx 服务端
    无 quota 语义、无 usage 字段 → quota/remaining = NULL（不得推导、不得伪造）

映射：
    balance   = total_balance（Provider 原始字段，按币种）
    quota     = NULL（DeepSeek 无 quota 概念）
    remaining = NULL（无 usage 字段，无法推导；绝不伪造）
    source    = "api"
    失败(401/429/超时/网络/解析) → status=error（消息经 sanitize，绝不泄露 key）

Secret 安全：key 只经 CredentialProvider 获取，绝不进入 Observation/DB/日志/错误消息。
"""
from __future__ import annotations

from typing import Optional

import httpx

from ..credential import CredentialProvider
from ..observe import ObservationCollector, ResourceObservation
from ..resource import ResourceDefinition
from ..sanitize import sanitize_error

BALANCE_URL = "https://api.deepseek.com/user/balance"


class DeepSeekObservationCollector(ObservationCollector):
    def __init__(self, credential_provider: Optional[CredentialProvider] = None,
                 client: Optional[httpx.Client] = None,
                 timeout: float = 10.0):
        self._cred = credential_provider or CredentialProvider()
        self._client = client      # 测试可注入 MockTransport client；None 则每次新建
        self._timeout = timeout

    def observe(self, resource: ResourceDefinition) -> ResourceObservation:
        # 1) 校验（Provider-specific 边界：只存在于 Collector，不进 Core）
        if resource.provider != "deepseek":
            raise ValueError(
                f"DeepSeekObservationCollector 不适用于 provider={resource.provider!r}")
        if not resource.enabled:
            return ResourceObservation(
                resource_id=resource.resource_id, status="unavailable",
                source="api", error="resource disabled")

        # 2) credential（引用 → 真实 secret，仅此处持有）
        key = self._cred.get(resource.credential_id or "deepseek")
        if not key:
            return ResourceObservation(
                resource_id=resource.resource_id, status="error",
                source="api", error="credential unavailable")

        # 3) 调用官方 API（有限 timeout，不阻塞 Monitor）
        try:
            client = self._client or httpx.Client(timeout=self._timeout)
            try:
                resp = client.get(
                    BALANCE_URL,
                    headers={"Authorization": f"Bearer {key}",
                             "Accept": "application/json"})
            finally:
                if self._client is None:
                    client.close()
        except httpx.HTTPError as e:
            return ResourceObservation(
                resource_id=resource.resource_id, status="error",
                source="api",
                error=sanitize_error(f"deepseek balance 请求失败: {type(e).__name__}"))

        # 4) 状态码处理（401/429/5xx 等）
        if resp.status_code != 200:
            return ResourceObservation(
                resource_id=resource.resource_id, status="error",
                source="api",
                error=sanitize_error(f"deepseek balance HTTP {resp.status_code}"))

        # 5) 解析（malformed JSON → error）
        try:
            data = resp.json() or {}
        except (ValueError, TypeError):
            return ResourceObservation(
                resource_id=resource.resource_id, status="error",
                source="api", error="deepseek balance 响应解析失败")

        # 6) 提取余额（DeepSeek 按币种返回 balance_infos；取第一条）
        infos = data.get("balance_infos") or []
        if not infos:
            return ResourceObservation(
                resource_id=resource.resource_id, status="error",
                source="api", error="deepseek balance 缺少 balance_infos")
        info = infos[0] if isinstance(infos[0], dict) else {}
        total_balance = info.get("total_balance")
        if total_balance is None:
            return ResourceObservation(
                resource_id=resource.resource_id, status="error",
                source="api", error="deepseek balance 缺少 total_balance")

        # 7) 构造 Observation（quota/remaining 恒 NULL：无 quota/usage 概念）
        try:
            balance = float(total_balance)
        except (ValueError, TypeError):
            return ResourceObservation(
                resource_id=resource.resource_id, status="error",
                source="api", error="deepseek balance total_balance 非数值")
        metadata = {
            "provider": "deepseek",
            "endpoint": "/user/balance",
            "currency": info.get("currency"),
            "is_available": data.get("is_available"),
            "granted_balance": info.get("granted_balance"),
            "topped_up_balance": info.get("topped_up_balance"),
        }
        return ResourceObservation(
            resource_id=resource.resource_id, status="known",
            balance=balance, quota=None, remaining=None,
            source="api", metadata=metadata)
