"""P0-3 测试：Resource-aware Analytics。

覆盖 6 场景 + 时间过滤 + 单资源详情 + 向后兼容。
"""
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402

from monitor.config import ConfigManager  # noqa: E402
from monitor.events import AIRequestEvent  # noqa: E402
from monitor.resource import ResourceDefinition, ResourceRegistry  # noqa: E402
from monitor.storage import EventStore  # noqa: E402


def _insert(store, *, provider="deepseek", model="deepseek-v4-flash",
            resource_id=None, input_tokens=100, output_tokens=50,
            cost=None, error=None, status_code=200, latency=100.0,
            timestamp=None):
    store.insert(AIRequestEvent(
        provider=provider, model=model, resource_id=resource_id,
        input_tokens=input_tokens, output_tokens=output_tokens,
        total_tokens=input_tokens + output_tokens,
        latency_ms=latency, status_code=status_code,
        cost=cost, error=error,
        timestamp=timestamp or time.time()))


@pytest.fixture()
def app_env(tmp_path, monkeypatch):
    import monitor.main as m
    from monitor.pricing import PricingRegistry

    cfg_path = tmp_path / "c.yaml"
    cfg_path.write_text(
        "providers:\n"
        "  deepseek:\n    enabled: true\n    base_url: http://up\n"
        "    api_keys:\n    - sk-test\n"
        "resources:\n"
        "  deepseek-paid:\n    provider: deepseek\n"
        "    resource_type: api\n    billing_mode: prepaid\n"
        "  deepseek-free:\n    provider: deepseek\n"
        "    resource_type: quota\n    billing_mode: free\n",
        encoding="utf-8")
    monkeypatch.setattr(m, "config_mgr", ConfigManager(cfg_path))
    monkeypatch.setattr(m, "store", EventStore(tmp_path / "t.db"))
    monkeypatch.setattr(m, "pricing", PricingRegistry())
    with TestClient(m.app) as c:
        yield c, m.store


def _usage(c, **kw):
    return c.get("/api/resources/usage", params=kw).json()


def _res(c, resource_id, **kw):
    return c.get(f"/api/resources/{resource_id}/usage", params=kw).json()


# ---------- Scenario A: Paid ----------
def test_paid_resource_known_cost(app_env):
    c, store = app_env
    _insert(store, resource_id="deepseek-paid", input_tokens=100,
            output_tokens=50, cost=0.001)
    data = _usage(c)
    paid = [r for r in data["resources"] if r["resource_id"] == "deepseek-paid"][0]
    assert paid["requests"] == 1
    assert paid["total_tokens"] == 150
    assert paid["cost"] == 0.001
    assert paid["cost_status"] == "known"


# ---------- Scenario B: Free quota（有使用无 cost）----------
def test_free_resource_unknown_cost(app_env):
    c, store = app_env
    _insert(store, resource_id="deepseek-free", input_tokens=200,
            output_tokens=100, cost=None)
    data = _usage(c)
    free = [r for r in data["resources"] if r["resource_id"] == "deepseek-free"][0]
    assert free["requests"] == 1
    assert free["total_tokens"] == 300
    assert free["cost"] is None        # 不是 0
    assert free["cost_status"] == "unknown"


# ---------- Scenario C: Mixed ----------
def test_mixed_cost_status(app_env):
    c, store = app_env
    _insert(store, resource_id="deepseek-paid", cost=0.01)
    _insert(store, resource_id="deepseek-paid", cost=None)
    data = _usage(c)
    paid = [r for r in data["resources"] if r["resource_id"] == "deepseek-paid"][0]
    assert paid["requests"] == 2
    assert paid["cost"] == 0.01        # 仅已知部分，明确 mixed
    assert paid["cost_status"] == "mixed"


# ---------- Scenario D: Unused Resource 仍可见 ----------
def test_unused_resource_still_appears(app_env):
    c, store = app_env
    _insert(store, resource_id="deepseek-paid", cost=0.01)
    data = _usage(c)
    ids = [r["resource_id"] for r in data["resources"]]
    assert "deepseek-free" in ids                # 无事件仍出现
    free = [r for r in data["resources"] if r["resource_id"] == "deepseek-free"][0]
    assert free["requests"] == 0 and free["total_tokens"] == 0
    assert free["cost_status"] == "none"         # 未使用，非 unknown


# ---------- Scenario E: Unattributed ----------
def test_unattributed_events_separate(app_env):
    c, store = app_env
    _insert(store, resource_id=None, input_tokens=10, cost=0.0001)
    _insert(store, resource_id="deepseek-paid", input_tokens=20, cost=0.0002)
    data = _usage(c)
    u = data["unattributed"]
    assert u["requests"] == 1
    assert u["total_tokens"] == 60        # input 10 + output 50（_insert 默认）
    assert u["cost"] == 0.0001
    # 未归因事件不进入任何具体 resource
    paid = [r for r in data["resources"] if r["resource_id"] == "deepseek-paid"][0]
    assert paid["requests"] == 1


# ---------- Scenario F: Same model, different resource ----------
def test_same_model_different_resource_not_merged(app_env):
    c, store = app_env
    _insert(store, resource_id="deepseek-paid", input_tokens=10, cost=0.01)
    _insert(store, resource_id="deepseek-free", input_tokens=999, cost=None)
    data = _usage(c)
    paid = [r for r in data["resources"] if r["resource_id"] == "deepseek-paid"][0]
    free = [r for r in data["resources"] if r["resource_id"] == "deepseek-free"][0]
    assert paid["total_tokens"] == 60 and paid["cost_status"] == "known"
    assert free["total_tokens"] == 1049 and free["cost_status"] == "unknown"


# ---------- Time Range ----------
def test_time_range_filtering(app_env):
    c, store = app_env
    old = time.time() - 40 * 86400
    _insert(store, resource_id="deepseek-paid", cost=0.5, timestamp=old)
    _insert(store, resource_id="deepseek-paid", cost=0.01)
    all_data = _usage(c)
    today_data = _usage(c, range="today")
    paid_all = [r for r in all_data["resources"]
                if r["resource_id"] == "deepseek-paid"][0]
    paid_today = [r for r in today_data["resources"]
                  if r["resource_id"] == "deepseek-paid"][0]
    assert paid_all["requests"] == 2
    assert paid_today["requests"] == 1


# ---------- Resource Detail ----------
def test_resource_detail(app_env):
    c, store = app_env
    _insert(store, resource_id="deepseek-paid", model="deepseek-v4-flash",
            input_tokens=100, output_tokens=50, cost=0.01)
    _insert(store, resource_id="deepseek-paid", model="deepseek-v4-pro",
            input_tokens=200, output_tokens=100, cost=0.02)
    d = _res(c, "deepseek-paid")
    assert d["resource"]["requests"] == 2
    models = {r["model"]: r for r in d["models"]}
    assert models["deepseek-v4-flash"]["requests"] == 1
    assert models["deepseek-v4-pro"]["requests"] == 1
    assert len(d["recent"]) == 2
    # 未知 resource → 404
    assert c.get("/api/resources/nope/usage").status_code == 404


# ---------- 向后兼容：历史 NULL 事件 ----------
def test_legacy_null_events_do_not_break(app_env):
    c, store = app_env
    _insert(store, resource_id=None, input_tokens=5)   # 历史事件（无 resource）
    _insert(store, resource_id="deepseek-paid", input_tokens=7, cost=0.0001)
    data = _usage(c)
    assert data["unattributed"]["requests"] == 1
    assert data["unattributed"]["total_tokens"] == 55   # input 5 + output 50
    # 现有 provider 聚合仍正常
    ov = c.get("/api/overview").json()
    assert ov["requests"] == 2
