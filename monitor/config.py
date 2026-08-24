"""Provider 独立配置：加载/保存 data/config.yaml。

安全约束：
- API Key 只存在本地配置文件内存中，不落数据库、不进日志、不下发前端。
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml

from .resource import ResourceDefinition


@dataclass
class ProviderConfig:
    name: str
    enabled: bool = False
    base_url: str = ""
    api_keys: list = field(default_factory=list)   # 多 Key（每个可对应不同模型/账号）
    test_model: str = ""          # verify 脚本使用的默认测试模型
    extra: dict = field(default_factory=dict)
    _key_index: int = field(default=0, repr=False)  # 轮询游标（运行态，不持久化）

    @property
    def api_key(self) -> str:
        """兼容旧代码：返回第一个 Key。"""
        return self.api_keys[0] if self.api_keys else ""

    @property
    def has_key(self) -> bool:
        return bool(self.api_keys)

    def get_active_key(self) -> str:
        """轮询选择 Key（多个 Key 时按请求轮流使用）。"""
        if not self.api_keys:
            return ""
        key = self.api_keys[self._key_index % len(self.api_keys)]
        self._key_index += 1
        return key


class ConfigManager:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._lock = threading.Lock()
        self.server: dict = {"host": "127.0.0.1", "port": 8787}
        self.sources: list[str] = []
        self.projects: list[str] = []
        self.scheduler: dict = {"enabled": True, "interval_seconds": 300}
        self.providers: dict[str, ProviderConfig] = {}
        self.resources: dict[str, ResourceDefinition] = {}
        self.load()

    def load(self) -> None:
        with self._lock:
            if self.path.exists():
                data = yaml.safe_load(self.path.read_text(encoding="utf-8")) or {}
            else:
                data = {}
            self.server = {**self.server, **(data.get("server") or {})}
            self.sources = [s for s in (data.get("sources") or []) if s]
            self.projects = [p for p in (data.get("projects") or []) if p]
            sch = data.get("scheduler") or {}
            self.scheduler = {
                "enabled": bool(sch.get("enabled", True)),
                "interval_seconds": max(1, int(sch.get("interval_seconds", 300))),
            }
            self.resources = {}
            for rid, r in (data.get("resources") or {}).items():
                r = r or {}
                known = {"name", "provider", "resource_type", "billing_mode",
                         "account_scope", "credential_id", "enabled"}
                self.resources[rid] = ResourceDefinition(
                    resource_id=rid,
                    name=r.get("name", "") or "",
                    provider=r.get("provider", "") or "",
                    resource_type=r.get("resource_type", "other") or "other",
                    billing_mode=r.get("billing_mode", "unknown") or "unknown",
                    account_scope=r.get("account_scope", "") or "",
                    credential_id=r.get("credential_id", "") or "",
                    enabled=bool(r.get("enabled", True)),
                    metadata={k: v for k, v in r.items() if k not in known},
                )
            self.providers = {}
            for name, p in (data.get("providers") or {}).items():
                p = p or {}
                known = {"enabled", "base_url", "api_keys", "api_key",
                         "test_model"}
                # 兼容旧版单 key（api_key）与新版多 key（api_keys）
                api_keys = [k for k in (p.get("api_keys") or []) if k]
                if not api_keys and p.get("api_key"):
                    api_keys = [p["api_key"]]
                self.providers[name] = ProviderConfig(
                    name=name,
                    enabled=bool(p.get("enabled", False)),
                    base_url=p.get("base_url", "") or "",
                    api_keys=api_keys,
                    test_model=p.get("test_model", "") or "",
                    extra={k: v for k, v in p.items() if k not in known},
                )

    def save(self) -> None:
        with self._lock:
            data = {
                "server": self.server,
                "providers": {
                    name: {
                        "enabled": p.enabled,
                        "base_url": p.base_url,
                        "api_keys": p.api_keys,
                        **({"test_model": p.test_model} if p.test_model else {}),
                        **p.extra,
                    }
                    for name, p in self.providers.items()
                },
            }
            if self.sources:
                data["sources"] = self.sources
            if self.projects:
                data["projects"] = self.projects
            data["scheduler"] = {
                "enabled": bool(self.scheduler.get("enabled", True)),
                "interval_seconds": max(
                    1, int(self.scheduler.get("interval_seconds", 300))),
            }
            if self.resources:
                data["resources"] = {
                    rid: {
                        **({"name": r.name} if r.name else {}),
                        **({"provider": r.provider} if r.provider else {}),
                        **({"resource_type": r.resource_type}
                           if r.resource_type != "other" else {}),
                        **({"billing_mode": r.billing_mode}
                           if r.billing_mode != "unknown" else {}),
                        **({"account_scope": r.account_scope}
                           if r.account_scope else {}),
                        **({"credential_id": r.credential_id}
                           if r.credential_id else {}),
                        **({"enabled": r.enabled} if not r.enabled else {}),
                        **r.metadata,
                    }
                    for rid, r in self.resources.items()
                }
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(
                yaml.safe_dump(data, allow_unicode=True, sort_keys=False),
                encoding="utf-8",
            )

    def set_dim_values(self, *, sources=None, projects=None) -> None:
        """Settings 页保存 Source/Project 预定义列表。"""
        with self._lock:
            if sources is not None:
                self.sources = [s.strip() for s in sources if s and s.strip()]
            if projects is not None:
                self.projects = [p.strip() for p in projects if p and p.strip()]
        self.save()

    def upsert_resource(self, definition: "ResourceDefinition") -> None:
        """新增/更新 Resource：修改内存 dict（Registry 活视图立即生效）+ 持久化。"""
        with self._lock:
            self.resources[definition.resource_id] = definition
        self.save()

    def delete_resource(self, resource_id: str) -> bool:
        """删除 Resource 定义（不影响历史 Event）。返回是否存在。"""
        with self._lock:
            removed = self.resources.pop(resource_id, None)
        if removed is not None:
            self.save()
        return removed is not None

    def get(self, name: str) -> Optional[ProviderConfig]:
        return self.providers.get(name)

    def upsert(self, name: str, *, enabled=None, base_url=None, api_key=None,
               api_keys=None, test_model=None) -> ProviderConfig:
        """Dashboard 配置入口。api_keys 显式传入时整体替换；
        api_key 传非空值时替换整个列表（兼容旧客户端）；
        api_key 传 None 表示保持不变。"""
        with self._lock:
            p = self.providers.get(name) or ProviderConfig(name=name)
            if enabled is not None:
                p.enabled = bool(enabled)
            if base_url is not None:
                p.base_url = base_url.strip().rstrip("/")
            if api_keys is not None:
                p.api_keys = [k.strip() for k in api_keys if k and k.strip()]
            elif api_key is not None:
                if api_key.strip():
                    p.api_keys = [api_key.strip()]
            if test_model is not None:
                p.test_model = test_model.strip()
            self.providers[name] = p
        self.save()
        return p

    def public_view(self) -> list[dict]:
        """给前端的视图：绝不包含 api_key 本体。"""
        return [
            {
                "name": p.name,
                "enabled": p.enabled,
                "base_url": p.base_url,
                "has_key": p.has_key,
                "key_count": len(p.api_keys),
                "test_model": p.test_model,
            }
            for p in self.providers.values()
        ]
