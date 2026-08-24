"""最小 Credential Provider（P0-8）。

原则：
- Secret 绝不进入 ResourceDefinition / Event / Observation / 日志 / API 响应。
- Resource 只保存 credential_id（引用），真实 secret 由本模块按约定从环境变量读取。
- 本实现刻意最小：不建 secret store / 不落盘 / 不加密 —— 环境变量即"最小安全机制"。

约定（文档化）：
    credential_id = "openrouter"       → 环境变量 OPENROUTER_API_KEY
    credential_id = "openrouter-main"  → 环境变量 OPENROUTER_MAIN_API_KEY
    通用规则：credential_id 大写、'-'→'_'、追加 _API_KEY
"""
from __future__ import annotations

import os
from typing import Optional

_ALIASES = {
    "openrouter": "OPENROUTER_API_KEY",
}


def _env_name(credential_id: str) -> str:
    if credential_id in _ALIASES:
        return _ALIASES[credential_id]
    return credential_id.upper().replace("-", "_") + "_API_KEY"


class CredentialProvider:
    """按 credential_id 解析真实 secret（仅环境变量，不落盘、不日志）。"""

    def get(self, credential_id: Optional[str]) -> Optional[str]:
        if not credential_id:
            return None
        val = os.environ.get(_env_name(credential_id))
        return val.strip() if val and val.strip() else None

    def available(self, credential_id: Optional[str]) -> bool:
        return self.get(credential_id) is not None
