"""Phase 3B 测试：Efficiency Derivation Layer。

核心纪律：宁可返回 NULL，也不把 NULL 当 0 / 估算 / 伪造。

覆盖：
1. NULL cost 不参与成本计算
2. NULL tokens 不参与 cost/token 比率
3. 0 请求 -> 比率 NULL
4. unknown pricing 不伪造成 0
5. failed request 不计入正常成本
6. cost coverage 正确
7. Resource 聚合正确
8. Provider 聚合正确
9. Model 聚合正确
10. cost/request
11. cost/1K tokens
12. tokens/request
13. error rate
14. latency (avg/p50/p95)
15. cache metric（仅语义成立时）
16. balance delta
17. balance delta 不得命名为 burn rate
"""
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from monitor.events import AIRequestEvent  # noqa: E402
from monitor.storage import EventStore  # noqa: E402


def _ev(store, *, provider="openai", model="gpt-4", resource_id=None,
        input_tokens=100, output_tokens=50, total_tokens=None,
        cost=None, currency=None, latency=100.0, error=None,
        status_code=200, cache_read_tokens=None, cache_hit=None):
    if total_tokens is None and input_tokens is not None \
            and output_tokens is not None:
        total_tokens = input_tokens + output_tokens
    store.insert(AIRequestEvent(
        provider=provider, model=model, resource_id=resource_id,
        input_tokens=input_tokens, output_tokens=output_tokens,
        total_tokens=total_tokens, cost=cost, currency=currency,
        latency_ms=latency, error=error, status_code=status_code,
        cache_read_tokens=cache_read_tokens, cache_hit=cache_hit,
        timestamp=time.time()))


@pytest.fixture()
def store(tmp_path):
    s = EventStore(tmp_path / "e.db")
    yield s
    s.close()


# ---------------- 基础：NULL 与覆盖度 ----------------

def test_null_cost_excluded_from_cost(store):
    # R1 cost=0.30, R2 cost=NULL -> 只有 R1 计入 cost
    _ev(store, resource_id="r1", cost=0.30, currency="USD")
    _ev(store, resource_id="r1", cost=None)  # unknown pricing
    ov = store.efficiency_overview()
    usd = ov["cost_by_currency"]["USD"]
    assert usd["known_cost"] == 0.30
    assert usd["priced_requests"] == 1
    assert usd["cost_coverage"] == 0.5  # 1/2


def test_unknown_pricing_not_faked_to_zero(store):
    _ev(store, cost=None)  # 全部 unknown
    ov = store.efficiency_overview()
    # 没有任何 known cost -> cost_by_currency 为空，绝不含 0
    assert ov["cost_by_currency"] == {}


def test_null_total_tokens_excluded_from_token_ratio(store):
    # R1 有 tokens，R2 total_tokens=NULL -> tokens_per_request 仅基于 tokened
    _ev(store, input_tokens=100, output_tokens=50, total_tokens=150)
    _ev(store, input_tokens=None, output_tokens=None, total_tokens=None)
    ov = store.efficiency_overview()
    tok = ov["tokens"]
    assert tok["tokened_requests"] == 1
    assert tok["tokens_per_request"] == 150.0
    assert tok["token_coverage"] == 0.5


def test_zero_requests_ratio_is_none(store):
    ov = store.efficiency_overview()
    assert ov["requests"] == 0
    assert ov["error_rate"] is None
    assert ov["tokens"]["tokens_per_request"] is None
    assert ov["cost_by_currency"] == {}
    assert ov["latency"]["avg_latency_ms"] is None
    assert ov["latency"]["p50_latency_ms"] is None
    assert ov["latency"]["p95_latency_ms"] is None


def test_failed_request_not_counted_as_normal_cost(store):
    # 失败请求（error + status 500）cost=NULL，应计入 errors 但不计入 cost
    _ev(store, provider="anthropic", error="boom", status_code=500,
        cost=None, latency=100.0)
    _ev(store, provider="anthropic", cost=0.50, currency="USD")
    rows = {r["name"]: r for r in store.efficiency_by_dim("provider")}
    anth = rows["anthropic"]
    assert anth["requests"] == 2
    assert anth["errors"] == 1
    assert anth["error_rate"] == 0.5
    assert anth["cost_by_currency"]["USD"]["known_cost"] == 0.50
    assert anth["cost_by_currency"]["USD"]["priced_requests"] == 1


# ---------------- 成本比率 ----------------

def test_cost_per_request_and_per_1k(store):
    # openai: R1 cost=0.30(150 tok), R3 cost=0.50(200 tok), R6 cost=1.0 CNY
    # R2 cost=NULL（不计入）
    _ev(store, provider="openai", model="gpt-4", resource_id="r1",
        input_tokens=100, output_tokens=50, cost=0.30, currency="USD")
    _ev(store, provider="openai", model="gpt-4", resource_id="r1",
        input_tokens=200, output_tokens=100, cost=None)
    _ev(store, provider="openai", model="gpt-4", resource_id="r2",
        input_tokens=100, output_tokens=100, cost=0.50, currency="USD")
    _ev(store, provider="openai", model="gpt-4", resource_id="r1",
        input_tokens=10, output_tokens=10, cost=1.0, currency="CNY")
    rows = {r["name"]: r for r in store.efficiency_by_dim("provider")}
    oai = rows["openai"]
    usd = oai["cost_by_currency"]["USD"]
    # 覆盖率：priced 3（R1,R3,R6）/ total 4
    assert usd["priced_requests"] == 2
    assert usd["cost_coverage"] == 0.5  # 2/4
    assert usd["known_cost"] == 0.80
    assert usd["cost_per_request"] == 0.40       # 0.80/2
    # priced_total_tokens = 150 + 200 = 350 -> 0.35 K
    assert usd["cost_per_1k_tokens"] == pytest.approx(0.80 / 0.35, rel=1e-3)
    # 多货币分离：CNY 独立
    cny = oai["cost_by_currency"]["CNY"]
    assert cny["known_cost"] == 1.0
    assert cny["cost_per_request"] == 1.0


def test_tokens_per_request(store):
    _ev(store, input_tokens=100, output_tokens=50, total_tokens=150)
    _ev(store, input_tokens=200, output_tokens=100, total_tokens=300)
    ov = store.efficiency_overview()
    assert ov["tokens"]["tokens_per_request"] == 225.0  # (150+300)/2
    assert ov["tokens"]["input_output_ratio"] == pytest.approx(300 / 150)  # 2.0
    # output_share 四舍五入到 4 位 = 0.3333；用容差比较避免浮点噪声
    assert ov["tokens"]["output_share"] == pytest.approx(150 / 450, abs=1e-3)


# ---------------- 维度聚合 ----------------

def test_resource_aggregation(store):
    _ev(store, resource_id="r1", cost=0.1, currency="USD")
    _ev(store, resource_id="r1", cost=0.2, currency="USD")
    _ev(store, resource_id="r2", cost=0.5, currency="USD")
    _ev(store, resource_id=None, cost=0.01, currency="USD")  # unattributed
    rows = {r["name"] for r in store.efficiency_by_dim("resource_id")}
    assert "r1" in rows and "r2" in rows and None in rows  # None=unattributed
    by = {r["name"]: r for r in store.efficiency_by_dim("resource_id")}
    assert by["r1"]["requests"] == 2
    assert by["r2"]["requests"] == 1
    assert by[None]["name"] is None


def test_provider_aggregation(store):
    _ev(store, provider="openai")
    _ev(store, provider="openai")
    _ev(store, provider="anthropic")
    by = {r["name"]: r for r in store.efficiency_by_dim("provider")}
    assert by["openai"]["requests"] == 2
    assert by["anthropic"]["requests"] == 1


def test_model_aggregation(store):
    _ev(store, model="gpt-4")
    _ev(store, model="gpt-4")
    _ev(store, model="claude")
    by = {r["name"]: r for r in store.efficiency_by_dim("model")}
    assert by["gpt-4"]["requests"] == 2
    assert by["claude"]["requests"] == 1


def test_error_rate_by_dim(store):
    _ev(store, provider="openai")
    _ev(store, provider="openai", error="x", status_code=500)
    by = {r["name"]: r for r in store.efficiency_by_dim("provider")}
    assert by["openai"]["error_rate"] == 0.5


def test_latency_percentiles(store):
    # 5 个延迟：10,20,30,40,50 -> p50=30, p95=50
    for v in (10, 20, 30, 40, 50):
        _ev(store, latency=float(v))
    # 再插一个无延迟事件（NULL latency）-> 不进入分位
    _ev(store, latency=None)
    ov = store.efficiency_overview()
    assert ov["latency"]["avg_latency_ms"] == 30.0
    assert ov["latency"]["p50_latency_ms"] == 30.0
    assert ov["latency"]["p95_latency_ms"] == 50.0


# ---------------- Cache（仅语义成立时） ----------------

def test_cache_metrics_only_when_data_present(store):
    # R1 cache_hit=0, R2 cache_hit=0, R3 cache_hit=1 -> 观测到 cache
    _ev(store, cache_read_tokens=0, cache_hit=0)
    _ev(store, cache_read_tokens=0, cache_hit=0)
    _ev(store, cache_read_tokens=50, cache_hit=1)
    ov = store.efficiency_overview()
    c = ov["cache"]
    assert c["cache_data_feasible"] is True
    assert c["cache_observable_requests"] == 3
    assert c["cache_hit_requests"] == 1
    assert c["cache_hit_rate"] == pytest.approx(1 / 3, abs=1e-3)
    # cache_read_ratio = cache_read_tokens / input_tokens（token 级复用占比）
    # 本测试：input 和 = 100*3 = 300，cache_read 和 = 50
    assert c["cache_read_ratio"] == pytest.approx(50 / 300, abs=1e-3)


def test_cache_not_feasible_when_no_data(store):
    # 没有任何 cache 数据 -> cache_hit 全 NULL -> not feasible
    _ev(store)
    _ev(store)
    ov = store.efficiency_overview()
    c = ov["cache"]
    assert c["cache_data_feasible"] is False
    assert c["cache_observable_requests"] == 0
    assert c["cache_hit_rate"] is None
    assert c["cache_read_ratio"] is None


# ---------------- Balance trend（非 burn rate） ----------------

def _obs(store, resource_id, balance, observed_at, status="known"):
    from monitor.observe import ResourceObservation
    obs = ResourceObservation(
        resource_id=resource_id, observed_at=observed_at, status=status,
        balance=balance, quota=None, remaining=None, reset_at=None,
        expires_at=None, source="test", error=None, metadata={})
    store.insert_observation(obs)


def test_balance_delta_computed_and_not_named_burn_rate(store):
    _obs(store, "acc", 100.0, 1000.0)
    _obs(store, "acc", 90.0, 2000.0)   # -10
    _obs(store, "acc", 70.0, 3000.0)   # -20
    _obs(store, "acc", None, 4000.0, status="error")  # 无 balance -> delta None
    trend = store.balance_trend("acc")
    pts = trend["points"]
    # points 按观测时间 DESC：4000(None) -> 3000(70) -> 2000(90) -> 1000(100)
    assert pts[0]["observed_at"] == 4000.0 and pts[0]["balance_delta"] is None
    assert pts[1]["balance_delta"] == -20.0   # 90 -> 70
    assert pts[2]["balance_delta"] == -10.0   # 100 -> 90
    assert pts[3]["balance_delta"] is None    # 首期无前值
    # 响应绝不出现 burn_rate 字段（note 仅以文字说明“不得命名为 burn rate”）
    assert "burn_rate" not in trend
    assert "burn_rate" not in trend["summary"]
    # known 余额按时间升序：100(t=1000) -> 70(t=3000)，观测差值为 -30（非消费速度）
    assert trend["summary"]["observed_balance_change"] == -30.0


def test_balance_trend_insufficient_points(store):
    _obs(store, "acc2", 100.0, 1000.0)  # 仅一个已知余额
    trend = store.balance_trend("acc2")
    assert trend["summary"]["observed_balance_change"] is None
    assert trend["summary"]["known_balance_points"] == 1
