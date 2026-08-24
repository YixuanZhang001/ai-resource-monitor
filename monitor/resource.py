"""Resource Registry — 最小资源定义与查询。

Resource = 用户实际拥有的 AI 资源（付费 API / 免费额度 / 订阅 / 本地模型等）。
与 Provider Registry（有哪些服务可接入）完全独立。

Registry 是 ConfigManager.resources 的**活视图**（共享同一 dict）：
ConfigManager.upsert_resource/delete_resource 修改 dict 并持久化后，
Registry 立即可见 —— 无需重启 Monitor。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

# 合法取值（校验用）
VALID_RESOURCE_TYPES = {
    "api", "credit", "quota", "subscription",
    "local_model", "local_compute", "other",
}
VALID_BILLING_MODES = {
    "free", "trial", "prepaid", "pay_as_you_go",
    "subscription", "local", "unknown",
}


@dataclass
class ResourceDefinition:
    resource_id: str          # 稳定唯一 ID（如 deepseek-paid）
    name: str = ""            # 用户可读名称
    provider: str = ""        # 来源（deepseek/openai/...）
    resource_type: str = "other"   # api|credit|quota|subscription|local_model|local_compute|other
    billing_mode: str = "unknown"  # free|trial|prepaid|pay_as_you_go|subscription|local|unknown
    account_scope: str = ""   # 逻辑账号标识（非 API Key 本身）
    credential_id: str = ""   # 凭据引用（如 "openrouter"）——真实 secret 走 CredentialProvider，绝不入此
    enabled: bool = True
    metadata: dict = field(default_factory=dict)


class ResourceRegistry:
    """资源查找：get / list / exists。

    注意：__init__ 持有 definitions 的**引用**（不是拷贝）。
    调用方（ConfigManager.resources）增删条目后，Registry 立即生效。
    """

    def __init__(self, definitions: Optional[dict] = None):
        self._resources = definitions if definitions is not None else {}

    def get(self, resource_id: str) -> Optional[ResourceDefinition]:
        return self._resources.get(resource_id)

    def exists(self, resource_id: str) -> bool:
        return resource_id in self._resources

    def list(self, enabled_only: bool = True) -> list[ResourceDefinition]:
        out = [r for r in self._resources.values()
               if (not enabled_only) or r.enabled]
        return sorted(out, key=lambda r: r.resource_id)

    def upsert(self, definition: ResourceDefinition) -> None:
        """直接修改共享 dict（不持久化；持久化走 ConfigManager.upsert_resource）。"""
        self._resources[definition.resource_id] = definition

    def remove(self, resource_id: str) -> bool:
        """从共享 dict 移除（不持久化）。返回是否存在。"""
        return self._resources.pop(resource_id, None) is not None

    def __len__(self) -> int:
        return len(self._resources)
