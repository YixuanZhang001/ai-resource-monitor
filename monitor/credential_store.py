"""本地 Manual Credential Store（P1 — Credential Access）。

与 EnvironmentCredentialSource 并列，作为 CredentialProvider 的第二个来源。

最小安全本地存储原则（与既有 Phase 1E-C 边界一致）：

- 凭据仅存本机 ``data/credentials.json``，绝不进入 config.yaml / Ledger /
  event metadata / 日志 / 前端响应 / Git。
- 文件创建后尽量收窄权限（0600），降低本地泄露面。
- 本模块不提供加密 / 密钥链 / 云同步 —— 它就是"本地文件"，与"环境变量是最小
  安全机制"同一哲学。需要更强机制时再单独评估。
- 优先级由 CredentialProvider 决定（Environment > Manual），本模块只负责
  "manual" 这一支的读写。
"""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Optional


class CredentialStore:
    """provider -> api_key 的本地存储（manual 来源）。

    键名即 gateway 解析凭据所用的标识（provider 名，如 "deepseek"），
    与 EnvironmentCredentialSource 的 ``_env_name`` 对齐。
    """

    def __init__(self, path):
        self.path = Path(path)
        self._lock = threading.Lock()

    # ---- 内部读写（锁内） ----
    def _load(self) -> dict:
        if not self.path.exists():
            return {}
        try:
            return json.loads(self.path.read_text(encoding="utf-8")) or {}
        except (ValueError, OSError):
            # 损坏文件不致命：视为空，后续 save 会覆盖。
            return {}

    def _dump(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(data, ensure_ascii=False),
                             encoding="utf-8")
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass  # 权限收窄失败不阻断功能（仅安全强化）

    # ---- 公开 API ----
    def save(self, provider: str, value: str) -> None:
        """保存 manual 凭据。空值拒绝（绝不存空串）。"""
        if not provider or not value or not value.strip():
            raise ValueError("credential value must not be empty")
        with self._lock:
            data = self._load()
            data[provider] = value.strip()
            self._dump(data)

    def get(self, provider: str) -> Optional[str]:
        with self._lock:
            data = self._load()
        v = data.get(provider)
        return v if v else None

    def available(self, provider: str) -> bool:
        return bool(self.get(provider))

    def delete(self, provider: str) -> bool:
        with self._lock:
            data = self._load()
            if provider in data:
                del data[provider]
                self._dump(data)
                return True
            return False

    def source(self, provider: str) -> Optional[str]:
        """manual 来源的标识；本模块永远返回 'manual' 或 None。"""
        return "manual" if self.available(provider) else None
