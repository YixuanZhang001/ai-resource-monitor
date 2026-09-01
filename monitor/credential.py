"""Credential Provider（P0-8 / P1）。

安全原则（贯穿 Phase 1E-C → P1）：
- Secret 绝不进入 ResourceDefinition / Event / Observation / 日志 / API 响应。
- Resource 只保存 credential_id（引用），真实 secret 由本模块按约定解析。
- 本实现刻意最小：不建 secret store / 不落盘 / 不日志。

P1 起支持两个来源（Credentials Access）：
1. Environment Variable（既有，优先级最高）
2. Manual（本地 data/credentials.json，由 CredentialStore 管理）

优先级：Environment Variable → Manual → Unavailable。
无论哪个来源命中，Resource attribution 都不依赖 secret（仍由 X-Monitor-Resource
或唯一 provider 回退决定），credential 只用于"认证"，绝不用于"身份识别"。
"""
from __future__ import annotations

import os
from typing import Optional

from .credential_store import CredentialStore

_ALIASES = {
    "openrouter": "OPENROUTER_API_KEY",
}


def _env_name(credential_id: str) -> str:
    if credential_id in _ALIASES:
        return _ALIASES[credential_id]
    return credential_id.upper().replace("-", "_") + "_API_KEY"


class CredentialProvider:
    """按 credential_id 解析真实 secret（环境变量为主，本地 manual 为辅）。

    store=None 时退化为纯环境变量解析（向后兼容既有调用方）。
    """

    def __init__(self, store: Optional[CredentialStore] = None):
        self.store = store

    def get(self, credential_id: Optional[str]) -> Optional[str]:
        if not credential_id:
            return None
        # 1) Environment Variable 优先
        env = os.environ.get(_env_name(credential_id))
        if env and env.strip():
            return env.strip()
        # 2) Manual（本地 store）
        if self.store is not None:
            return self.store.get(credential_id)
        return None

    def source(self, credential_id: Optional[str]) -> Optional[str]:
        """返回实际命中来源：'environment' / 'manual' / None。

        Environment 优先：即使 manual 也已存储，只要环境变量存在就报 environment。
        manual 不会被静默删除或覆盖。
        """
        if not credential_id:
            return None
        env = os.environ.get(_env_name(credential_id))
        if env and env.strip():
            return "environment"
        if self.store is not None and self.store.available(credential_id):
            return "manual"
        return None

    def available(self, credential_id: Optional[str]) -> bool:
        return self.get(credential_id) is not None
