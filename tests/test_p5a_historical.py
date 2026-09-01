"""P5-A：Data Reliability & User Convenience — 历史数据可见性测试。

核心断言：
- 历史 Ledger 事件（含 input/output/total/cache tokens、cost、resource_id）必须能被
  聚合并展示，且不依赖「今日是否发起请求」。
- NULL 成本绝不显示为 0；多货币 cost 分别计价、不求和。
- 严格区分 REGISTERED / UNREGISTERED / UNATTRIBUTED；0 使用资源 = none，不伪造。
"""
import time
from pathlib import Path

import pytest

sys_path = str(Path(__file__).resolve().parent.parent)
import sys  # noqa: E402
sys.path.insert(0, sys_path)

from fastapi.testclient import TestClient  # noqa: E402

from monitor.events import AIRequestEvent  # noqa: E402
import monitor.main as m  # noqa: E402
from monitor.config import ConfigManager  # noqa: E402
from monitor.resource import ResourceRegistry  # noqa: E402
from monitor.storage import EventStore  # noqa: E402


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(m, "config_mgr", ConfigManager(tmp_path / "config.yaml"))
    monkeypatch.setattr(m, "store", EventStore(tmp_path / "test.db"))
    monkeypatch.setattr(m, "resources",
                        ResourceRegistry(m.config_mgr.resources))
    with TestClient(m.app) as c:
        yield c, m.store


def ins(store, *, provider="deepseek", model="deepseek-chat", source=None,
        project=None, input_tokens=100, output_tokens=50, total_tokens=None,
        latency_ms=100.0, status_code=200, cost=None, currency=None, error=None,
        timestamp=None, request_id=None, resource_id=None, cache_read_tokens=0):
    e = AIRequestEvent(
        provider=provider, model=model, source=source, project=project,
        input_tokens=input_tokens, output_tokens=output_tokens,
        total_tokens=total_tokens or (input_tokens + output_tokens),
        latency_ms=latency_ms, status_code=status_code, cost=cost,
        currency=currency, error=error, resource_id=resource_id,
        cache_read_tokens=cache_read_tokens)
    if timestamp is not None:
        e.timestamp = timestamp
    if request_id:
        e.request_id = request_id
    store.insert(e)
    return e.request_id


# ---------- 1. 历史 token 聚合（input/output/total/cache） ----------

def test_overview_historical_tokens(client):
    c, store = client
    now = time.time()
    ins(store, provider="deepseek", input_tokens=1000, output_tokens=500,
        cache_read_tokens=200, cost=0.002, currency="CNY", timestamp=now)
    ins(store, provider="qwen", input_tokens=500, output_tokens=250,
        cache_read_tokens=0, cost=0.001, currency="CNY", timestamp=now)
    ins(store, provider="kimi", input_tokens=10, output_tokens=5,
        cache_read_tokens=0, status_code=401, error="bad key",
        timestamp=now - 10 * 86400)  # 10 天前（历史）
    ov = c.get("/api/overview", params={"range": "all"}).json()
    assert ov["requests"] == 3
    assert ov["input_tokens"] == 1510
    assert ov["output_tokens"] == 755
    assert ov["total_tokens"] == 2265
    assert ov["cache_read_tokens"] == 200
    assert ov["errors"] == 1
    assert ov["cost_by_currency"] == {"CNY": 0.003}


def test_overview_range_excludes_old_but_all_includes(client):
    c, store = client
    now = time.time()
    ins(store, provider="deepseek", input_tokens=1000, output_tokens=500,
        cost=0.002, currency="CNY", timestamp=now)            # 今天
    ins(store, provider="kimi", input_tokens=10, output_tokens=5,
        timestamp=now - 10 * 86400)                          # 历史（10 天前）
    today = c.get("/api/overview", params={"range": "today"}).json()
    all_t = c.get("/api/overview", params={"range": "all"}).json()
    assert today["requests"] == 1
    assert today["input_tokens"] == 1000
    assert all_t["requests"] == 2                            # 历史事件仍计入 all
    assert all_t["input_tokens"] == 1010


# ---------- 2. 空 DB：不伪造 ----------

def test_overview_empty_db(client):
    c, store = client
    ov = c.get("/api/overview", params={"range": "all"}).json()
    assert ov["requests"] == 0
    assert ov["input_tokens"] == 0
    assert ov["output_tokens"] == 0
    assert ov["total_tokens"] == 0
    assert ov["cache_read_tokens"] == 0
    # 未知成本 → 空 dict，绝不 {CNY: 0}
    assert ov["cost_by_currency"] == {}


# ---------- 3. 多货币：cost 分别计价，不求和 ----------

def test_overview_mixed_currency_not_summed(client):
    c, store = client
    ins(store, provider="deepseek", cost=0.002, currency="CNY")
    ins(store, provider="openai", cost=0.001, currency="USD")
    ov = c.get("/api/overview", params={"range": "all"}).json()
    assert ov["cost_by_currency"] == {"CNY": 0.002, "USD": 0.001}


# ---------- 4. Attribution 严格分离 ----------

def test_resource_attribution_split(client):
    c, store = client
    now = time.time()
    # 注册 r1 + r2
    c.post("/api/resources", json={"resource_id": "r1", "name": "R1",
                                   "provider": "deepseek", "resource_type": "api",
                                   "billing_mode": "prepaid"})
    c.post("/api/resources", json={"resource_id": "r2", "name": "R2",
                                   "provider": "openai", "resource_type": "api",
                                   "billing_mode": "pay_as_you_go"})
    # r1 有历史事件（5 天前，证明非「今日」）
    ins(store, resource_id="r1", input_tokens=100, output_tokens=50,
        cost=0.01, currency="CNY", timestamp=now - 5 * 86400)
    # 未归因（resource_id=None）
    ins(store, resource_id=None, input_tokens=20, output_tokens=10,
        timestamp=now - 5 * 86400)
    # 孤儿（已删除/从未注册）
    ins(store, resource_id="ghost", input_tokens=7, output_tokens=3,
        timestamp=now - 5 * 86400)
    # r2 无任何事件 → 0 使用

    d = c.get("/api/resources/usage", params={"range": "all"}).json()
    by_id = {r["resource_id"]: r for r in d["resources"]}
    assert by_id["r1"]["requests"] == 1
    assert by_id["r1"]["total_tokens"] == 150
    assert by_id["r1"]["cost_status"] == "known"
    assert by_id["r1"]["cost"] == 0.01
    # 0 使用资源：none，cost None
    assert by_id["r2"]["requests"] == 0
    assert by_id["r2"]["cost_status"] == "none"
    assert by_id["r2"]["cost"] is None
    # 未归因
    assert d["unattributed"]["requests"] == 1
    assert d["unattributed"]["total_tokens"] == 30
    # 孤儿（unregistered）
    ghosts = [u for u in d["unregistered"] if u["resource_id"] == "ghost"]
    assert ghosts and ghosts[0]["requests"] == 1
    assert ghosts[0]["total_tokens"] == 10


# ---------- 5. Resource Detail 历史聚合与 Recent Requests 分离 ----------

def test_resource_detail_historical_vs_recent(client):
    c, store = client
    c.post("/api/resources", json={"resource_id": "r1", "name": "R1",
                                   "provider": "deepseek", "resource_type": "api",
                                   "billing_mode": "prepaid"})
    now = time.time()
    # 两条历史事件（均 5 天前）
    ins(store, resource_id="r1", input_tokens=100, output_tokens=50,
        cost=0.01, currency="CNY", timestamp=now - 5 * 86400)
    ins(store, resource_id="r1", input_tokens=80, output_tokens=40,
        timestamp=now - 4 * 86400)
    det = c.get("/api/resources/r1/usage", params={"range": "all"}).json()
    assert det["resource"]["requests"] == 2
    assert det["resource"]["input_tokens"] == 180
    assert det["resource"]["output_tokens"] == 90
    assert det["resource"]["total_tokens"] == 270
    # recent 仅最近 10 条（此处 2 条）
    assert len(det["recent"]) == 2
    # 即使今天没有新请求，历史仍可见
    assert det["resource"]["requests"] == 2
