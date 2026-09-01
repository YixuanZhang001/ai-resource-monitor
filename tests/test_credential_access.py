"""P1 — Credential Access 测试（Environment + Manual UI input）。

仅使用伪造凭据（FAKE），绝不接触真实 API Key / 真实 data/monitor.db / config.yaml。
通过 fixture 把 store / credentials / scheduler 全部重定向到 tmp，保证零污染。
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from monitor.credential import CredentialProvider
from monitor.credential_store import CredentialStore
from monitor.storage import EventStore

import monitor.main as main_mod
from monitor import gateway as gw

FAKE = "test-deepseek-key-123"   # 伪造值，仅用于断言"不泄露"


# ============ 单元：Environment Source ============

def test_env_detection(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", FAKE)
    p = CredentialProvider()
    assert p.get("deepseek") == FAKE
    assert p.source("deepseek") == "environment"
    assert p.available("deepseek") is True


def test_env_absence(monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    p = CredentialProvider()
    assert p.get("deepseek") is None
    assert p.source("deepseek") is None
    assert p.available("deepseek") is False


def test_env_generic_mapping(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", FAKE)
    assert CredentialProvider().get("openai") == FAKE
    # 别名路径
    monkeypatch.setenv("OPENROUTER_API_KEY", "rk-test")
    assert CredentialProvider().get("openrouter") == "rk-test"


# ============ 单元：Manual Source (CredentialStore) ============

def test_manual_save_retrieve(tmp_path):
    store = CredentialStore(tmp_path / "cred.json")
    store.save("deepseek", FAKE)
    assert store.get("deepseek") == FAKE
    assert store.available("deepseek") is True
    assert store.source("deepseek") == "manual"
    with pytest.raises(ValueError):
        store.save("deepseek", "   ")   # 空值拒绝


def test_manual_delete(tmp_path):
    store = CredentialStore(tmp_path / "cred.json")
    assert store.delete("deepseek") is False
    store.save("deepseek", FAKE)
    assert store.delete("deepseek") is True
    assert store.get("deepseek") is None


# ============ 单元：CredentialProvider 组合 + 优先级 ============

def test_provider_manual_only(tmp_path, monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    store = CredentialStore(tmp_path / "cred.json")
    store.save("deepseek", FAKE)
    p = CredentialProvider(store)
    assert p.get("deepseek") == FAKE
    assert p.source("deepseek") == "manual"


def test_provider_env_precedence(tmp_path, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "env-key")
    store = CredentialStore(tmp_path / "cred.json")
    store.save("deepseek", FAKE)          # manual 也存了
    p = CredentialProvider(store)
    assert p.get("deepseek") == "env-key"   # 环境变量优先
    assert p.source("deepseek") == "environment"
    assert store.get("deepseek") == FAKE   # manual 未被删除/覆盖


def test_provider_without_store_ignores_manual(tmp_path, monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    p = CredentialProvider()              # 无 store
    assert p.get("deepseek") is None


# ============ 单元：Credential 绝不驱动 Attribution ============

def test_attribution_independent_of_credential():
    """Resource 归因由 header/registry 决定，与凭据无关（函数根本不接收 credential）。"""
    from monitor.resource import ResourceRegistry
    reg = ResourceRegistry(main_mod.config_mgr.resources)
    # 无 header + deepseek 有 2 个 enabled -> 未归因（NULL）
    rid, reject, _ = gw._resolve_resource_id("deepseek", None, reg)
    assert rid is None and reject is None
    # 合法 header -> 映射到资源（与凭据是否存在无关）
    rid, reject, _ = gw._resolve_resource_id("deepseek", "deepseek-paid", reg)
    assert rid == "deepseek-paid" and reject is None
    # 未知 header -> 400（与凭据无关）
    rid, reject, _ = gw._resolve_resource_id("deepseek", "nope", reg)
    assert rid is None and reject is not None and reject.status_code == 400


# ============ 集成：API 行为 + 泄露审计 ============

class _StubScheduler:
    def __init__(self, *a, **k): pass
    def start(self): pass
    async def stop(self): pass
    def status(self): return {"enabled": False}


@pytest.fixture
def client(monkeypatch, tmp_path):
    # 全部重定向到 tmp，确保零污染真实 data/
    monkeypatch.setattr(main_mod, "CredentialStore",
                        lambda path: CredentialStore(tmp_path / "cred.json"))
    monkeypatch.setattr(main_mod, "store", EventStore(tmp_path / "monitor.db"))
    monkeypatch.setattr(main_mod, "ObservationScheduler", _StubScheduler)
    monkeypatch.setattr(main_mod.config_mgr, "scheduler",
                        {"enabled": False, "interval_seconds": 9999})
    yield TestClient(main_mod.app)


def test_credentials_put_get_delete(client, monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    with client as c:
        r = c.put("/api/credentials/deepseek", json={"api_key": FAKE})
        assert r.status_code == 200
        body = r.json()
        assert body["credential_available"] is True
        assert body["credential_source"] == "manual"
        assert "api_key" not in body          # 绝不回显
        # providers 列表反映 source
        provs = {p["name"]: p for p in c.get("/api/providers").json()["providers"]}
        assert provs["deepseek"]["credential_source"] == "manual"
        assert provs["deepseek"]["has_key"] is True
        # 清除
        assert c.delete("/api/credentials/deepseek").json()["removed"] is True
        provs = {p["name"]: p for p in c.get("/api/providers").json()["providers"]}
        assert provs["deepseek"]["credential_source"] is None


def test_credentials_unknown_provider(client):
    with client as c:
        assert c.put("/api/credentials/nope", json={"api_key": FAKE}).status_code == 404


def test_resource_api_never_returns_credential(client, monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    with client as c:
        c.put("/api/credentials/deepseek", json={"api_key": FAKE})
        for path in ("/api/providers", "/api/resources", "/api/resources/usage"):
            assert FAKE not in c.get(path).text, f"credential leaked in {path}"
        # 创建 Resource 的响应也不含 key
        r = c.post("/api/resources",
                   json={"resource_id": "t-r", "name": "T", "provider": "deepseek"})
        assert FAKE not in r.text


def test_missing_credential_safe_failure(client, monkeypatch):
    """无 env、无 manual -> Gateway 返回 400 安全信息，且不泄露 key。"""
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    with client as c:
        c.delete("/api/credentials/deepseek")   # 确保无 manual
        r = c.post("/gateway/deepseek/v1/chat/completions",
                   json={"model": "deepseek-chat", "messages": []},
                   headers={"x-monitor-resource": "deepseek-paid"})
        assert r.status_code == 400
        assert FAKE not in r.text
        # 安全信息引用的是环境变量【名】，而非【值】
        assert "DEEPSEEK_API_KEY" in r.text
