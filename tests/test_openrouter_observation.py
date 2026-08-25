"""P0-8 测试：OpenRouter Observation Collector（mock HTTP，不调真实 API）。

覆盖 15 场景：success/remaining 推导/missing remaining/401/403/timeout/provider
error/malformed/sanitize/disabled/unknown→unavailable/registry routing/storage/
latest/history/manual 回归。
"""
import sys
import time
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402

from monitor.collectors.openrouter import (  # noqa: E402
    CREDITS_URL, OpenRouterObservationCollector)
from monitor.config import ConfigManager  # noqa: E402
from monitor.observe import ObservationCollectorRegistry  # noqa: E402
from monitor.resource import (  # noqa: E402
    ResourceDefinition, ResourceRegistry)
from monitor.storage import EventStore  # noqa: E402

SECRET = "sk-or-test-secret"


def _collector(handler, credential=SECRET):
    """带 MockTransport 的 Collector（真实 HTTP 客户端，mock 传输层）。"""
    client = httpx.Client(transport=httpx.MockTransport(handler), timeout=5)

    class _Cred:
        def get(self, cid):
            return credential if credential is not None else None

        def available(self, cid):
            return credential is not None

    return OpenRouterObservationCollector(credential_provider=_Cred(),
                                          client=client)


def _json_handler(status=200, payload=None, raise_exc=None, hits=None):
    def handler(request):
        if hits is not None:
            hits.append(request.url)
        if raise_exc is not None:
            raise raise_exc
        return httpx.Response(status, json=payload or {}, request=request)
    return handler


def _resource(**kw):
    base = dict(resource_id="or-main", provider="openrouter",
                credential_id="openrouter")
    base.update(kw)
    return ResourceDefinition(**base)


# ---------- 1-3. 成功 / remaining 推导 / missing ----------

def test_successful_credits_response():
    c = _collector(_json_handler(
        200, {"data": {"total_credits": 100, "total_usage": 25}}))
    o = c.observe(_resource())
    assert o.status == "known"
    assert o.balance == 100.0
    assert o.remaining == 75.0                    # 推导
    assert o.quota is None                        # OpenRouter 无 quota 概念
    assert o.source == "api"
    assert o.metadata["remaining_source"] == "derived"
    assert o.metadata["endpoint"] == "/api/v1/credits"


def test_remaining_derived_not_provider_field():
    # remaining 必须标注为 derived（不得伪装成 Provider 原始字段）
    c = _collector(_json_handler(
        200, {"data": {"total_credits": 50.0, "total_usage": 12.35}}))
    o = c.observe(_resource())
    assert o.remaining == round(50.0 - 12.35, 6)
    assert o.metadata["remaining_source"] == "derived"
    assert o.metadata["total_usage"] == 12.35


def test_missing_remaining():
    # total_usage 缺失 → remaining=NULL，但仍 known（核心数据已知）
    c = _collector(_json_handler(200, {"data": {"total_credits": 100}}))
    o = c.observe(_resource())
    assert o.status == "known" and o.balance == 100.0
    assert o.remaining is None
    assert o.metadata["remaining_source"] is None


# ---------- 4-7. 错误场景 ----------

def test_invalid_credential_401():
    c = _collector(_json_handler(
        401, {"error": {"code": 401, "message": "Unauthorized"}}))
    o = c.observe(_resource())
    assert o.status == "error"
    assert SECRET not in o.error


def test_forbidden_403():
    c = _collector(_json_handler(
        403, {"error": {"code": 403, "message": "Only management keys"}}))
    o = c.observe(_resource())
    assert o.status == "error" and SECRET not in (o.error or "")


def test_timeout():
    c = _collector(_json_handler(raise_exc=httpx.ReadTimeout("boom")))
    o = c.observe(_resource())
    assert o.status == "error"
    assert "ReadTimeout" in o.error


def test_provider_error_500():
    c = _collector(_json_handler(500, {"error": {"message": "server"}}))
    o = c.observe(_resource())
    assert o.status == "error" and "500" in o.error


# ---------- 8-9. 解析与脱敏 ----------

def test_malformed_json():
    def handler(request):
        return httpx.Response(200, text="<html>not json</html>", request=request)
    c = _collector(handler)
    o = c.observe(_resource())
    assert o.status == "error"


def test_secret_sanitization():
    # 即使上游错误消息回显 key 尾缀，也不得进入 Observation error
    def handler(request):
        return httpx.Response(401, json={
            "error": {"message": f"invalid key {SECRET}123 | org-x"}},
            request=request)
    c = _collector(handler)
    o = c.observe(_resource())
    assert o.status == "error"
    assert SECRET not in (o.error or "")
    assert "sk-" not in (o.error or "").lower()


# ---------- 10. disabled ----------

def test_disabled_resource_no_request():
    hits = []
    c = _collector(_json_handler(200, {"data": {"total_credits": 1}}, hits=hits))
    o = c.observe(_resource(enabled=False))
    assert o.status == "unavailable"
    assert o.error == "resource disabled"
    assert hits == []                              # 未发起任何请求


# ---------- 11-12. Registry 路由 ----------

def test_registry_routing():
    reg = ObservationCollectorRegistry()
    c = _collector(_json_handler(200, {"data": {"total_credits": 1}}))
    reg.register("openrouter", c)
    assert reg.get("openrouter") is c
    assert reg.get("deepseek") is None             # 无 collector
    assert "openrouter" in reg.providers()


def test_unknown_provider_no_collector_unavailable(app_env):
    """auto observe：无 collector 的 provider（gemini）→ unavailable（不 500）。"""
    c, m = app_env
    # gemini 资源无 collector（deepseek 已在 Phase 2C 注册 collector）
    r = c.post("/api/resources/gemini-res/observe", json={})
    assert r.status_code == 200
    assert r.json()["observation_status"] == "unavailable"
    assert "no observation collector" in r.json()["error"]


# ---------- 13-14. Storage / latest / history（经 API 全链路） ----------

def test_api_auto_routing_known(app_env, monkeypatch):
    c, m = app_env
    hits = []
    mock_c = _collector(_json_handler(
        200, {"data": {"total_credits": 100, "total_usage": 20}}, hits=hits))
    monkeypatch.setattr(m.observation_collectors, "register",
                        lambda p, col: None)      # 不覆盖 openrouter
    m.observation_collectors._collectors["openrouter"] = mock_c
    r = c.post("/api/resources/or-res/observe", json={})     # auto
    assert r.status_code == 200
    d = r.json()
    assert d["observation_status"] == "known"
    assert d["balance"] == 100.0 and d["remaining"] == 80.0
    assert d["source"] == "api"
    # storage 已写入
    assert m.store.latest_observation("or-res")["status"] == "known"
    assert m.store.latest_observation("or-res")["source"] == "api"


def test_historical_snapshots_preserved(app_env, monkeypatch):
    c, m = app_env
    m.observation_collectors._collectors["openrouter"] = _collector(
        _json_handler(200, {"data": {"total_credits": 100, "total_usage": 20}}))
    c.post("/api/resources/or-res/observe", json={})
    time.sleep(0.01)
    c.post("/api/resources/or-res/observe", json={})
    snaps = m.store.observations_for("or-res", 50)
    assert len(snaps) == 2                          # 两次观察两个时间点
    assert snaps[0]["observed_at"] >= snaps[1]["observed_at"]


# ---------- 15. Manual 回归 ----------

def test_manual_observation_still_works(app_env):
    c, m = app_env
    r = c.post("/api/resources/deepseek-res/observe",
               json={"status": "known", "balance": 55, "remaining": 44})
    assert r.status_code == 200
    assert r.json()["observation_status"] == "known"
    assert r.json()["balance"] == 55.0
    assert r.json()["source"] == "manual"


# ---------- fixture ----------

@pytest.fixture()
def app_env(tmp_path, monkeypatch):
    import monitor.main as m
    cfg_path = tmp_path / "c.yaml"
    cfg_path.write_text(
        "scheduler: {enabled: false}\n"
        "providers:\n  deepseek:\n    enabled: true\n"
        "    base_url: 'http://up'\n    api_keys:\n    - sk-test\n"
        "resources:\n"
        "  deepseek-res:\n    provider: deepseek\n    resource_type: api\n"
        "    billing_mode: prepaid\n"
        "  gemini-res:\n    provider: gemini\n    resource_type: api\n"
        "    billing_mode: prepaid\n"
        "  or-res:\n    provider: openrouter\n    resource_type: api\n"
        "    billing_mode: prepaid\n    credential_id: openrouter\n",
        encoding="utf-8")
    monkeypatch.setattr(m, "config_mgr", ConfigManager(cfg_path))
    monkeypatch.setattr(m, "store", EventStore(tmp_path / "t.db"))
    monkeypatch.setattr(m, "resources", ResourceRegistry(m.config_mgr.resources))
    with TestClient(m.app) as c:
        yield c, m


def test_credential_id_roundtrip(app_env):
    """credential_id 存于 ResourceDefinition（引用，非 secret），可持久化。"""
    c, m = app_env
    rd = m.resources.get("or-res")
    assert rd.credential_id == "openrouter"
    assert SECRET not in c.get("/api/resources/or-res").text


def test_wrong_provider_collector_rejected():
    c = _collector(_json_handler(200, {"data": {"total_credits": 1}}))
    with pytest.raises(ValueError):
        c.observe(_resource(provider="deepseek"))
