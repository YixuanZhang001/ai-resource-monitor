"""缓存节省按日期选价 + 通用时间序列（tokens_series）测试。

覆盖三件事（均为隔离临时 DB，绝不触碰生产库）：

1) PricingRegistry.cache_diff：按 effective_date 选生效版本的 (miss−hit) 价差，
   未配置 cache_hit → None（不估算、不伪造）。
2) EventStore.cache_savings：通过 resolver 按事件「当天生效价」估算，
   历史（08-17 前档）与现行（08-17 后档）分别计价；多币种不求和。
3) EventStore.tokens_series：任意窗口 + day/hour 粒度 + 缺档零填充；
   与 provider / 采集方式无关（通用范式）。
"""
import time
from datetime import datetime

import pytest

from monitor.events import AIRequestEvent
from monitor.pricing import PricingRegistry
from monitor.storage import EventStore


@pytest.fixture()
def store(tmp_path):
    s = EventStore(tmp_path / "t.db")
    s.migrate()
    return s


def _ts(month: int, day: int, hour: int = 12) -> float:
    return datetime(2026, month, day, hour, 0, 0).timestamp()


def _day_epoch(day: str) -> float:
    """与 main._cache_resolver 一致：day 字符串 → 当天正午 epoch。"""
    return time.mktime(time.strptime(day, "%Y-%m-%d")) + 12 * 3600


def _insert(store, ts, cache_read=1_000_000, model="deepseek-v4-flash",
            provider="deepseek", input_tokens=2_000_000, output_tokens=100_000):
    store.insert(AIRequestEvent(
        provider=provider, model=model, timestamp=ts,
        input_tokens=input_tokens, output_tokens=output_tokens,
        total_tokens=input_tokens + output_tokens,
        cache_read_tokens=cache_read, status_code=200))


# ---------- 1. cache_diff：按日期选生效价 ----------

def test_cache_diff_version_before_0817():
    d = PricingRegistry().cache_diff("deepseek", "deepseek-v4-flash", _ts(8, 10))
    assert d == pytest.approx((1.0 - 0.02, "CNY"))


def test_cache_diff_version_on_and_after_0817():
    reg = PricingRegistry()
    assert reg.cache_diff("deepseek", "deepseek-v4-flash", _ts(8, 17)) == \
        pytest.approx((1.5 - 0.05, "CNY"))
    assert reg.cache_diff("deepseek", "deepseek-v4-flash", _ts(9, 1)) == \
        pytest.approx((1.5 - 0.05, "CNY"))


def test_cache_diff_unpriced_model_returns_none():
    # yaml 中无 cache_hit 的模型 → None，绝不猜价
    assert PricingRegistry().cache_diff("openai", "gpt-4o", _ts(9, 1)) is None


# ---------- 2. cache_savings：按事件当天生效价估算 ----------

def test_cache_savings_date_aware(store):
    _insert(store, _ts(8, 10))   # 08-17 前档：价差 0.98
    _insert(store, _ts(8, 20))   # 08-17 后空闲档：价差 1.45
    reg = PricingRegistry()

    def resolver(provider, model, day):
        return reg.cache_diff(provider, model, _day_epoch(day))

    out = store.cache_savings(cache_resolver=resolver)
    # (1M × 0.98 + 1M × 1.45) / 1e6·1M → 2.43 元
    assert out["total"] == pytest.approx(2.43, abs=1e-6)
    assert out["currency"] == "CNY"
    assert out["unpriced"] == []


def test_cache_savings_unpriced_provider_marked(store):
    _insert(store, _ts(8, 20), provider="some-new-api",
            model="new-model-x")  # yaml/config 均无 cache_hit
    out = store.cache_savings(cache_resolver=lambda *a: None)
    assert out["total"] == 0.0
    assert "some-new-api" in out["unpriced"]


def test_cache_savings_multi_currency_no_cross_sum(store):
    class _Reg:
        def cache_diff(self, provider, model, at):
            if provider == "deepseek":
                return (1.0, "CNY")
            return (2.0, "USD")

    _insert(store, _ts(8, 20))
    _insert(store, _ts(8, 20), provider="other", model="m2")
    out = store.cache_savings(cache_resolver=lambda p, m, d: _Reg().cache_diff(p, m, None))
    assert out["total"] is None            # 多币种不求和
    assert out["by_currency"] == {"CNY": pytest.approx(1.0, abs=1e-6),
                                  "USD": pytest.approx(2.0, abs=1e-6)}


# ---------- 3. tokens_series：任意窗口 + 粒度 + 零填充 ----------

def test_tokens_series_day_zero_fill(store):
    now = time.time()
    _insert(store, now - 100)
    rows = store.tokens_series(start=now - 3 * 86400, end=now, granularity="day")
    assert len(rows) == 4                            # 含首尾共 4 个日桶
    assert sum(r["requests"] for r in rows) == 1     # 只有 1 条真实事件
    empty = [r for r in rows if r["requests"] == 0]
    assert all(r["input_tokens"] == 0 and r["cache_read_tokens"] == 0 for r in empty)


def test_tokens_series_hour_buckets(store):
    start = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    _insert(store, time.time() - 60)
    rows = store.tokens_series(start=start, end=time.time(), granularity="hour")
    assert len(rows) == datetime.now().hour + 1      # 当日 00:00 起每小时一桶
    assert rows[-1]["requests"] == 1
    assert all(r["day"].endswith(":00") for r in rows)


def test_tokens_series_window_filtering(store):
    # 窗口外的事件不进入聚合
    _insert(store, _ts(8, 10))
    rows = store.tokens_series(start=_ts(9, 1) - 86400, end=_ts(9, 1), granularity="day")
    assert sum(r["requests"] for r in rows) == 0


def test_tokens_series_rejects_bad_granularity(store):
    with pytest.raises(ValueError):
        store.tokens_series(start=0, end=time.time(), granularity="week")


# ---------- 4. API 端点：granularity 分支与参数校验 ----------

@pytest.fixture()
def app(tmp_path, monkeypatch):
    from monitor.config import ConfigManager
    import monitor.main as m
    cfg = tmp_path / "c.yaml"
    cfg.write_text(
        "server: {host: 127.0.0.1, port: 8787}\n"
        "providers:\n  deepseek:\n    enabled: true\n    base_url: 'http://up'\n",
        encoding="utf-8")
    monkeypatch.setattr(m, "config_mgr", ConfigManager(cfg))
    monkeypatch.setattr(m, "store", EventStore(tmp_path / "t.db"))
    m.store.migrate()
    from fastapi.testclient import TestClient
    with TestClient(m.app) as c:
        yield c


def test_api_timeseries_hour_granularity(app):
    start = datetime.now().replace(hour=0, minute=0, second=0,
                                   microsecond=0).timestamp()
    r = app.get("/api/analytics/tokens/timeseries",
                params={"start": start, "end": time.time(), "granularity": "hour"})
    assert r.status_code == 200
    rows = r.json()["rows"]
    assert rows and rows[0]["day"].endswith(":00")


def test_api_timeseries_rejects_bad_granularity(app):
    r = app.get("/api/analytics/tokens/timeseries", params={"granularity": "week"})
    assert r.status_code == 400


def test_api_timeseries_default_backcompat(app):
    r = app.get("/api/analytics/tokens/timeseries", params={"days": 7})
    assert r.status_code == 200
    assert isinstance(r.json()["rows"], list)
