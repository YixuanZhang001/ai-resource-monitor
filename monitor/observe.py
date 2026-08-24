"""Resource Observation Framework — 最小 Observation 抽象。

Resource Observation = 某个时间点 Resource 自身状态的快照（与 Usage Event 分离：
Usage Event 记录"发生了一次使用"，Observation 记录"现在这个 Resource 的状态"）。

status 语义（四态，互不混淆）：
  no_observation : 该 Resource 从未产生 Observation（= 无任何记录，不是 0）
  known          : Collector 成功获得状态数据（balance/quota/remaining 可为 0，0≠NULL）
  unavailable    : Collector 明确知道该状态对此 Resource 不可用（如本地模型无余额概念）
  error          : 本来应该能观察，但 Collector 执行失败

禁止：
  - no_observation → 0
  - NULL → 0
  - cost 当 balance
  - free 当 remaining=0
"""
from __future__ import annotations

import time
from abc import ABC
from dataclasses import dataclass, field
from typing import Optional

OBS_STATUSES = {"no_observation", "known", "unavailable", "error"}


@dataclass
class ResourceObservation:
    resource_id: str
    observed_at: float = field(default_factory=time.time)
    status: str = "no_observation"   # no_observation|known|unavailable|error
    balance: Optional[float] = None  # NULL = 未观察/不可用；0 = 确实为 0
    quota: Optional[float] = None
    remaining: Optional[float] = None
    reset_at: Optional[float] = None
    expires_at: Optional[float] = None
    source: Optional[str] = None     # 'manual'|'api'|...
    error: Optional[str] = None      # 已脱敏
    metadata: Optional[dict] = None  # JSON 兜底

    def to_dict(self) -> dict:
        return {
            "resource_id": self.resource_id,
            "observed_at": self.observed_at,
            "status": self.status,
            "balance": self.balance,
            "quota": self.quota,
            "remaining": self.remaining,
            "reset_at": self.reset_at,
            "expires_at": self.expires_at,
            "source": self.source,
            "error": self.error,
            "metadata": self.metadata,
        }


class ObservationCollector(ABC):
    """Collector 抽象：Resource → ResourceObservation。

    Provider-specific 逻辑（balance/quota API 调用）只允许出现在
    Collector 的具体实现里；Core/Analytics/Dashboard 不感知来源。
    """

    def observe(self, resource) -> ResourceObservation:
        raise NotImplementedError


class ManualObservationCollector(ObservationCollector):
    """非 Provider-specific 的手工/测试采集实现（验证路径）。

    用户显式提交状态数据 → 构造 ResourceObservation。
    不做任何自动猜测；status 必须显式给出。
    """

    def __init__(self, payload: dict):
        self._payload = payload or {}

    def observe(self, resource) -> ResourceObservation:
        status = self._payload.get("status", "no_observation")
        if status not in OBS_STATUSES:
            raise ValueError(f"invalid observation status: {status!r}")
        # 非 known 状态不允许携带数值（NULL ≠ 0；unavailable/error 无数值语义）
        values = {}
        if status == "known":
            for k in ("balance", "quota", "remaining", "reset_at", "expires_at"):
                v = self._payload.get(k)
                if v is not None:
                    values[k] = float(v)
        return ResourceObservation(
            resource_id=resource.resource_id,
            status=status,
            source=self._payload.get("source") or "manual",
            error=self._payload.get("error"),
            metadata=self._payload.get("metadata"),
            **values,
        )


class ObservationCollectorRegistry:
    """Collector Registry / Factory：resource.provider → collector。

    禁止在 main.py 写 provider 分支；新 Provider 只需 register(provider, collector)。
    get() 返回 None = 无 collector（调用方应产出 status=unavailable，而非 500）。
    """

    def __init__(self):
        self._collectors: dict[str, ObservationCollector] = {}

    def register(self, provider: str, collector: ObservationCollector) -> None:
        self._collectors[provider] = collector

    def get(self, provider: str) -> Optional[ObservationCollector]:
        return self._collectors.get(provider)

    def providers(self) -> list[str]:
        return sorted(self._collectors)
