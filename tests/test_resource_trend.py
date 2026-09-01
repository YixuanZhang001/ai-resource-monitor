"""Phase 4 Step 1 测试：Resource-level daily time-series。

覆盖：
1. 多日 Resource 事件 → 正确的每日 request 计数
2. 带 token 数据 → 正确的每日 total_tokens
3. 已知 cost → 正确的每日 cost
4. 多货币 cost → 货币保持分离（不求和）
5. 未知 cost → 保持 NULL/unknown，绝不为 0
6. NULL resource_id 事件 → 保持未归因，不分配给任何 Resource
7. 无事件的 Resource → 返回空 rows（符合现有 API 约定）
8. days 参数 → 正确限制时间范围
9. 无 schema 变更（纯 events 聚合，无新表）
10. 现有测试保持 green（本文件独立运行）

不触碰生产 data/monitor.db；全部使用 tmp_path 隔离数据库。
"""
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402

from monitor.config import ConfigManager  # noqa: E402
from monitor.events import AIRequestEvent  # noqa: E402
from monitor.storage import EventStore  # noqa: E402


def _insert(store, *, resource_id=None, input_tokens=100, output_tokens=50,
            cost=None, currency=None, error=None, status_code=200,
            latency=100.0, event_type="llm_call", timestamp=None):
    store.insert(AIRequestEvent(
        provider="deepseek", model="deepseek-v4-flash",
        resource_id=resource_id,
        input_tokens=input_tokens, output_tokens=output_tokens,
        total_tokens=input_tokens + output_tokens,
        latency_ms=latency, status_code=status_code,
        cost=cost, currency=currency,
        error=error, event_type=event_type,
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


def _ts(days_ago, hour=12):
    """生成 days_ago 天前的 epoch 秒（固定本地时区，便于 day 分组断言）。"""
    return time.time() - days_ago * 86400 - (12 - hour) * 3600


# ---------- 1. 多日 Resource 事件 → 每日 request 计数 ----------

def test_multi_day_request_counts(app_env):
    c, store = app_env
    _insert(store, resource_id="deepseek-paid", timestamp=_ts(3))
    _insert(store, resource_id="deepseek-paid", timestamp=_ts(3))
    _insert(store, resource_id="deepseek-paid", timestamp=_ts(1))
    data = c.get("/api/resources/deepseek-paid/timeseries",
                 params={"days": 10}).json()
    days = {r["day"]: r["requests"] for r in data["rows"]}
    # 两个不同 day 各应有对应计数
    assert sum(days.values()) == 3
    assert len(days) == 2


# ---------- 2. 带 token 数据 → 每日 total_tokens ----------

def test_daily_total_tokens(app_env):
    c, store = app_env
    # day 2 天前：单事件 input=100 output=50 → total 150
    _insert(store, resource_id="deepseek-paid", input_tokens=100,
            output_tokens=50, timestamp=_ts(2))
    # day 1 天前：两事件各 input=200 output=100 → total 600
    _insert(store, resource_id="deepseek-paid", input_tokens=200,
            output_tokens=100, timestamp=_ts(1))
    _insert(store, resource_id="deepseek-paid", input_tokens=200,
            output_tokens=100, timestamp=_ts(1))
    data = c.get("/api/resources/deepseek-paid/timeseries",
                 params={"days": 10}).json()
    by_day = {r["day"]: r for r in data["rows"]}
    assert len(by_day) == 2
    # 每个 day 的 total_tokens 与 requests 一致
    for r in data["rows"]:
        assert r["total_tokens"] == r["input_tokens"] + r["output_tokens"]


# ---------- 3. 已知 cost → 每日 cost ----------

def test_daily_known_cost(app_env):
    c, store = app_env
    _insert(store, resource_id="deepseek-paid", cost=0.01, currency="CNY",
            timestamp=_ts(2))
    _insert(store, resource_id="deepseek-paid", cost=0.02, currency="CNY",
            timestamp=_ts(2))
    _insert(store, resource_id="deepseek-paid", cost=0.05, currency="CNY",
            timestamp=_ts(1))
    data = c.get("/api/resources/deepseek-paid/timeseries",
                 params={"days": 10}).json()
    by_day = {r["day"]: r for r in data["rows"]}
    assert len(by_day) == 2
    totals = [r["cost"] for r in data["rows"]]
    # 单货币：cost 便利标量应等于该日 CNY 求和（0.03 与 0.05）
    flat = sorted(totals)
    assert flat == [0.03, 0.05]
    # 同时 cost_by_currency 完整保留
    for r in data["rows"]:
        assert r["cost_by_currency"] == {"CNY": r["cost"]}


# ---------- 4. 多货币 cost → 货币保持分离 ----------

def test_multi_currency_separated(app_env):
    c, store = app_env
    _insert(store, resource_id="deepseek-paid", cost=0.01, currency="CNY",
            timestamp=_ts(1))
    _insert(store, resource_id="deepseek-paid", cost=0.02, currency="USD",
            timestamp=_ts(1))
    data = c.get("/api/resources/deepseek-paid/timeseries",
                 params={"days": 10}).json()
    assert len(data["rows"]) == 1
    r = data["rows"][0]
    # 多货币：cost 便利标量为 None（绝不求和），cost_by_currency 保留两份
    assert r["cost"] is None
    assert r["cost_by_currency"] == {"CNY": 0.01, "USD": 0.02}


# ---------- 5. 未知 cost → 保持 NULL/unknown，绝不为 0 ----------

def test_unknown_cost_stays_null(app_env):
    c, store = app_env
    _insert(store, resource_id="deepseek-paid", cost=None, currency=None,
            timestamp=_ts(1))
    _insert(store, resource_id="deepseek-paid", cost=None, currency=None,
            timestamp=_ts(1))
    data = c.get("/api/resources/deepseek-paid/timeseries",
                 params={"days": 10}).json()
    assert len(data["rows"]) == 1
    r = data["rows"][0]
    assert r["cost"] is None
    assert r["cost_by_currency"] is None
    assert r["requests"] == 2        # 有使用，但 cost 不伪装为 0


# ---------- 6. NULL resource_id → 保持未归因，不分配 ----------

def test_null_resource_id_unattributed_separate(app_env):
    c, store = app_env
    _insert(store, resource_id=None, cost=0.0001, currency="CNY",
            timestamp=_ts(1))
    _insert(store, resource_id="deepseek-paid", cost=0.0002, currency="CNY",
            timestamp=_ts(1))
    # per-resource 端点必须不含 NULL 事件
    paid = c.get("/api/resources/deepseek-paid/timeseries",
                 params={"days": 10}).json()
    assert paid["rows"][0]["requests"] == 1
    assert paid["rows"][0]["cost"] == 0.0002
    # 显式未归因端点包含 NULL 事件，且单独成桶
    unatt = c.get("/api/resources/unattributed/timeseries",
                  params={"days": 10}).json()
    assert unatt["resource_id"] is None
    assert len(unatt["rows"]) == 1
    assert unatt["rows"][0]["requests"] == 1
    assert unatt["rows"][0]["cost"] == 0.0001


# ---------- 7. 无事件 → 空 rows（符合现有 API 约定） ----------

def test_no_events_empty_rows(app_env):
    c, store = app_env
    # 未注册且无事件的 resource_id
    data = c.get("/api/resources/never-used/timeseries",
                 params={"days": 10}).json()
    assert data["resource_id"] == "never-used"
    assert data["rows"] == []


# ---------- 8. days 参数 → 正确限制时间范围 ----------

def test_days_parameter_limits_range(app_env):
    c, store = app_env
    _insert(store, resource_id="deepseek-paid", cost=0.5, currency="CNY",
            timestamp=_ts(40))      # 40 天前
    _insert(store, resource_id="deepseek-paid", cost=0.01, currency="CNY",
            timestamp=_ts(1))       # 1 天前
    all_d = c.get("/api/resources/deepseek-paid/timeseries",
                  params={"days": 60}).json()
    near_d = c.get("/api/resources/deepseek-paid/timeseries",
                   params={"days": 10}).json()
    assert len(all_d["rows"]) == 2
    assert len(near_d["rows"]) == 1
    assert near_d["rows"][0]["cost"] == 0.01


# ---------- 9. 无 schema 变更（无新表） ----------

def test_no_new_table(app_env):
    c, store = app_env
    import sqlite3
    con = sqlite3.connect(str(store.db_path))
    tables = [r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")]
    con.close()
    # 仅既有表，未新增 trend 表
    assert "events" in tables
    assert "resource_states" in tables
    assert not any("trend" in t for t in tables)


# ---------- 10. 错误/延迟聚合随趋势返回（稳定性维度） ----------

def test_errors_and_latency_in_row(app_env):
    c, store = app_env
    _insert(store, resource_id="deepseek-paid", status_code=200, latency=100.0,
            timestamp=_ts(1))
    _insert(store, resource_id="deepseek-paid", status_code=500,
            error="boom", latency=300.0, timestamp=_ts(1))
    data = c.get("/api/resources/deepseek-paid/timeseries",
                 params={"days": 10}).json()
    r = data["rows"][0]
    assert r["requests"] == 2
    assert r["errors"] == 1
    # avg_latency = (100+300)/2 = 200.0
    assert r["avg_latency_ms"] == 200.0
