"""Phase 2C 测试：DeepSeek Observation Collector（mock HTTP，不调真实 API）。

覆盖：success/balance 写入/quota=NULL/remaining=NULL/missing total/empty infos/
401/429/timeout/credential unavailable/secret 脱敏/disabled/provider 校验/
registry 路由/scheduler 发现/API 全链路状态可见/env credential。

绝不降低现有测试数量；本文件为新增。
"""
import asyncio
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402

from monitor.collectors.deepseek import (  # noqa: E402
    BALANCE_URL, DeepSeekObservationCollector)
from monitor.config import ConfigManager  # noqa: E402
from monitor.observe import (  # noqa: E402
    ObservationCollectorRegistry, ResourceObservation)
from monitor.resource import (  # noqa: E402
    ResourceDefinition, ResourceRegistry)
from monitor.scheduler import ObservationScheduler  # noqa: E402
from monitor.storage import EventStore  # noqa: E402

SECRET = "sk-ds-test-secret"
OK_PAYLOAD = {
    "is_available": True,
    "balance_infos": [{
        "currency": "CNY",
        "total_balance": "110.00",
        "granted_balance": "10.00",
        "topped_up_balance": "100.00",
    }],
}


def _collector(handler, credential=SECRET):
    """带 MockTransport 的 Collector（真实 httpx 客户端，mock 传输层）。"""
    client = httpx.Client(transport=httpx.MockTransport(handler), timeout=5)

    class _Cred:
        def get(self, cid):
            return credential if credential is not None else None

        def available(self, cid):
            return credential is not None

    return DeepSeekObservationCollector(credential_provider=_Cred(), client=client)


def _json_handler(status=200, payload=None, raise_exc=None, hits=None):
    def handler(request):
        if hits is not None:
            hits.append(request.url)
        if raise_exc is not None:
            raise raise_exc
        return httpx.Response(status, json=payload or {}, request=request)
    return handler


def _resource(**kw):
    base = dict(resource_id="ds-main", provider="deepseek",
                credential_id="deepseek")
    base.update(kw)
    return ResourceDefinition(**base)


# ---------- 1-3. 成功 / balance / quota+remaining NULL ----------

def test_successful_balance_response():
    c = _collector(_json_handler(200, OK_PAYLOAD))
    o = c.observe(_resource())
    assert o.status == "known"
    assert o.balance == 110.0
    assert o.quota is None                       # DeepSeek 无 quota 概念
    assert o.remaining is None                   # 无 usage 字段，绝不推导/伪造
    assert o.source == "api"
    assert o.metadata["provider"] == "deepseek"
    assert o.metadata["endpoint"] == "/user/balance"
    assert o.metadata["currency"] == "CNY"
    assert o.metadata["is_available"] is True


def test_quota_and_remaining_are_null_not_zero():
    c = _collector(_json_handler(200, OK_PAYLOAD))
    o = c.observe(_resource())
    assert o.quota is None and o.remaining is None
    # 关键：不得把 NULL 当成 0
    assert o.quota != 0 and o.remaining != 0


def test_unavailable_balance_is_zero_not_null():
    # is_available=false 但 total_balance=0 → known + 真实 0（区分于 no_observation）
    payload = {"is_available": False,
               "balance_infos": [{"currency": "USD", "total_balance": "0.00"}]}
    c = _collector(_json_handler(200, payload))
    o = c.observe(_resource())
    assert o.status == "known"
    assert o.balance == 0.0                       # 真实零，非 NULL
    assert o.metadata["is_available"] is False


# ---------- 4-8. 错误 / 缺失场景 ----------

def test_missing_total_balance_error():
    payload = {"balance_infos": [{"currency": "CNY"}]}   # 缺 total_balance
    c = _collector(_json_handler(200, payload))
    o = c.observe(_resource())
    assert o.status == "error"
    assert SECRET not in (o.error or "")


def test_empty_balance_infos_error():
    c = _collector(_json_handler(200, {"balance_infos": []}))
    o = c.observe(_resource())
    assert o.status == "error"


def test_invalid_credential_401():
    c = _collector(_json_handler(
        401, {"error": {"message": "Unauthorized"}}))
    o = c.observe(_resource())
    assert o.status == "error"
    assert SECRET not in (o.error or "")


def test_rate_limit_429_no_fake_balance():
    c = _collector(_json_handler(429, {"error": {"message": "rate"}}))
    o = c.observe(_resource())
    assert o.status == "error"
    assert o.balance is None                      # 失败绝不伪造成余额


def test_timeout_error():
    c = _collector(_json_handler(raise_exc=httpx.ReadTimeout("boom")))
    o = c.observe(_resource())
    assert o.status == "error"
    assert "ReadTimeout" in (o.error or "")


def test_credential_unavailable_error():
    c = _collector(_json_handler(200, OK_PAYLOAD), credential=None)
    o = c.observe(_resource())
    assert o.status == "error"
    assert o.error == "credential unavailable"


def test_secret_sanitization():
    # 即使上游错误消息回显 key，也不得进入 Observation error
    def handler(request):
        return httpx.Response(401, json={
            "error": {"message": f"invalid key {SECRET}123 | org-x"}},
            request=request)
    c = _collector(handler)
    o = c.observe(_resource())
    assert o.status == "error"
    assert SECRET not in (o.error or "")
    assert "sk-" not in (o.error or "").lower()


def test_disabled_resource_no_request():
    hits = []
    c = _collector(_json_handler(200, OK_PAYLOAD, hits=hits))
    o = c.observe(_resource(enabled=False))
    assert o.status == "unavailable"
    assert o.error == "resource disabled"
    assert hits == []                             # 未发起任何请求


def test_wrong_provider_rejected():
    c = _collector(_json_handler(200, OK_PAYLOAD))
    with pytest.raises(ValueError):
        c.observe(_resource(provider="openai"))


# ---------- 9-10. Registry / Scheduler 发现 ----------

def test_registry_routing_registered():
    reg = ObservationCollectorRegistry()
    c = _collector(_json_handler(200, OK_PAYLOAD))
    reg.register("deepseek", c)
    assert reg.get("deepseek") is c
    assert "deepseek" in reg.providers()


def test_scheduler_discovers_deepseek(tmp_path):
    reg = ObservationCollectorRegistry()
    mock_c = _collector(_json_handler(
        200, {"balance_infos": [{"currency": "USD", "total_balance": "42.5"}]}))
    reg.register("deepseek", mock_c)
    res = ResourceRegistry({
        "ds1": ResourceDefinition(resource_id="ds1", provider="deepseek",
                                  credential_id="deepseek")})
    store = EventStore(tmp_path / "s.db")
    sched = ObservationScheduler(res, reg, store, {"enabled": False})
    asyncio.run(sched._cycle())
    snap = store.latest_observation("ds1")
    assert snap["status"] == "known"
    assert snap["balance"] == 42.5
    assert snap["remaining"] is None               # scheduler 也保持 NULL


# ---------- 11-12. API 全链路 + env credential ----------

def _ds_app(tmp_path, monkeypatch):
    import monitor.main as m
    cfg_path = tmp_path / "c.yaml"
    cfg_path.write_text(
        "scheduler: {enabled: false}\n"
        "providers:\n  deepseek:\n    enabled: true\n"
        "    base_url: 'http://up'\n    api_keys:\n    - sk-test\n"
        "resources:\n"
        "  ds-res:\n    provider: deepseek\n    resource_type: api\n"
        "    billing_mode: prepaid\n    credential_id: deepseek\n",
        encoding="utf-8")
    monkeypatch.setattr(m, "config_mgr", ConfigManager(cfg_path))
    monkeypatch.setattr(m, "store", EventStore(tmp_path / "t.db"))
    monkeypatch.setattr(m, "resources", ResourceRegistry(m.config_mgr.resources))
    return m


def test_api_auto_observation_known(app_env_alias):
    c, m = app_env_alias
    mock_c = _collector(_json_handler(200, OK_PAYLOAD))
    m.observation_collectors._collectors["deepseek"] = mock_c
    r = c.post("/api/resources/ds-res/observe", json={})     # auto
    assert r.status_code == 200
    d = r.json()
    assert d["observation_status"] == "known"
    assert d["balance"] == 110.0
    assert d["remaining"] is None
    assert d["source"] == "api"
    # storage 已写入
    assert m.store.latest_observation("ds-res")["status"] == "known"


def test_api_state_shows_deepseek_observation(app_env_alias):
    c, m = app_env_alias
    m.observation_collectors._collectors["deepseek"] = _collector(
        _json_handler(200, OK_PAYLOAD))
    c.post("/api/resources/ds-res/observe", json={})
    r = c.get("/api/resources/ds-res/state")
    assert r.status_code == 200
    d = r.json()
    assert d["observation_status"] == "known"
    assert d["balance"] == 110.0
    assert d["quota"] is None
    assert d["remaining"] is None                 # Dashboard 将显示 —


def test_env_credential_used(app_env_alias, monkeypatch):
    """真实 CredentialProvider 走环境变量，不出现 secret 泄漏。"""
    monkeypatch.setenv("DEEPSEEK_API_KEY", SECRET)
    import monitor.credential as cred_mod
    prov = cred_mod.CredentialProvider()
    assert prov.get("deepseek") == SECRET
    c, m = app_env_alias
    # 用真实 CredentialProvider + mock http 验证 env 链路
    from monitor.collectors.deepseek import DeepSeekObservationCollector
    mock_client = httpx.Client(
        transport=httpx.MockTransport(_json_handler(200, OK_PAYLOAD)), timeout=5)
    real_col = DeepSeekObservationCollector(
        credential_provider=prov, client=mock_client)
    o = real_col.observe(ResourceDefinition(
        resource_id="ds-env", provider="deepseek", credential_id="deepseek"))
    assert o.status == "known"
    assert o.balance == 110.0


@pytest.fixture()
def app_env_alias(tmp_path, monkeypatch):
    m = _ds_app(tmp_path, monkeypatch)
    with TestClient(m.app) as c:
        yield c, m


def test_collector_module_constants():
    assert BALANCE_URL == "https://api.deepseek.com/user/balance"
