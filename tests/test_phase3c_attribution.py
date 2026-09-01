"""Phase 3C：Resource Attribution + 最小 Health 派生。

范围严格受限（经 Audit 验证）：
- MUST：Resource Attribution（X-Monitor-Resource 权威 + 唯一 enabled 回退）
- SHOULD：最小 Health 派生（基于真实 status/error，非探测）
- DEFER：rate-limit（本环境无 credential，未验证）
- DO NOT：credential→resource 映射、历史回填、新 event_type、schema 变更

覆盖：header 归因 / 唯一回退 / 拒绝无效·禁用 / 空值 / 多 provider·多 resource /
历史 NULL 不变 / Resource-level analytics（token/cost/error/latency/efficiency）/
NULL 与多币种处理 / health 三态 + 分离。
"""
import sys
import json
import time
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402

from monitor.config import ConfigManager  # noqa: E402
from monitor.events import AIRequestEvent  # noqa: E402
from monitor.storage import EventStore  # noqa: E402
from monitor.resource import ResourceDefinition, ResourceRegistry  # noqa: E402
import monitor.gateway as gateway_mod  # noqa: E402
from monitor.gateway import _resolve_resource_id  # noqa: E402


# ---------- fixtures ----------

def make_upstream():
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            length = int(self.headers.get("content-length") or 0)
            body = json.loads(self.rfile.read(length) or b"{}")
            data = json.dumps({
                "id": "chatcmpl-1",
                "model": body.get("model") or "deepseek-v4-flash",
                "choices": [{"message": {"role": "assistant",
                                         "content": "ok"}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5,
                          "total_tokens": 15},
            }).encode()
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


@pytest.fixture()
def gw_e2e(tmp_path, monkeypatch):
    """带 mock upstream 的网关环境（复制自 test_resource_mgmt 模式）。"""
    import monitor.main as m

    CFG = ("scheduler: {enabled: false}\n"
           "providers:\n"
           "  deepseek:\n    enabled: true\n    base_url: http://up\n"
           "    api_keys:\n    - sk-test\n")
    cfg_path = tmp_path / "c.yaml"
    cfg_path.write_text(CFG, encoding="utf-8")
    monkeypatch.setattr(m, "config_mgr", ConfigManager(cfg_path))
    monkeypatch.setattr(m, "store", EventStore(tmp_path / "t.db"))
    monkeypatch.setattr(m, "resources",
                        ResourceRegistry(m.config_mgr.resources))
    upstream = make_upstream()
    m.config_mgr.upsert("deepseek", enabled=True,
                        base_url=f"http://127.0.0.1:{upstream.server_port}",
                        api_key="sk-test")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
    yield m
    upstream.shutdown()


@pytest.fixture
def reg_single():
    return ResourceRegistry({
        "ds-only": ResourceDefinition(resource_id="ds-only",
                                      provider="deepseek", enabled=True),
    })


@pytest.fixture
def reg_multi():
    return ResourceRegistry({
        "ds-a": ResourceDefinition(resource_id="ds-a", provider="deepseek",
                                   enabled=True),
        "ds-b": ResourceDefinition(resource_id="ds-b", provider="deepseek",
                                   enabled=True),
    })


# ---------- helpers ----------

def _seed(store, rid, *, n=1, cost=None, currency="CNY", in_t=100, out_t=50,
         status=200, error=None, latency=120.0, total=None):
    if total is None and in_t is not None and out_t is not None:
        total = in_t + out_t
    for _ in range(n):
        store.insert(AIRequestEvent(
            provider="deepseek", model="deepseek-chat", resource_id=rid,
            input_tokens=in_t, output_tokens=out_t, total_tokens=total,
            cost=cost, currency=currency, latency_ms=latency,
            status_code=status, error=error))


# ============================================================
# A. Resource Attribution（helper 层，纯函数）
# ============================================================

def test_1_valid_header_attribution(reg_single):
    rid, err, _ = _resolve_resource_id("deepseek", "ds-only", reg_single)
    assert rid == "ds-only" and err is None


def test_2_missing_header_unique_fallback(reg_single):
    rid, err, _ = _resolve_resource_id("deepseek", None, reg_single)
    assert rid == "ds-only" and err is None


def test_3_missing_header_multiple_resources_null(reg_multi):
    rid, err, _ = _resolve_resource_id("deepseek", None, reg_multi)
    assert rid is None and err is None


def test_4_invalid_resource_rejected():
    reg = ResourceRegistry(
        {"ds": ResourceDefinition(resource_id="ds", provider="deepseek")})
    rid, err, _ = _resolve_resource_id("deepseek", "nope", reg)
    assert rid is None and err is not None and err.status_code == 400
    assert "unknown resource" in err.body.decode()


def test_5_disabled_resource_rejected():
    reg = ResourceRegistry(
        {"ds": ResourceDefinition(resource_id="ds", provider="deepseek",
                                   enabled=False)})
    rid, err, _ = _resolve_resource_id("deepseek", "ds", reg)
    assert rid is None and err is not None and err.status_code == 400
    assert "disabled" in err.body.decode()


def test_6_empty_header_falls_through(reg_single):
    # 空字符串不得作为 resource_id 落库；回退到唯一 enabled
    rid, err, _ = _resolve_resource_id("deepseek", "", reg_single)
    assert rid == "ds-only" and err is None


def test_7_multiple_providers_independent_fallback():
    reg = ResourceRegistry({
        "oa": ResourceDefinition(resource_id="oa", provider="openai",
                                  enabled=True),
        "ds-a": ResourceDefinition(resource_id="ds-a", provider="deepseek",
                                    enabled=True),
        "ds-b": ResourceDefinition(resource_id="ds-b", provider="deepseek",
                                    enabled=True),
    })
    rid_o, _, _ = _resolve_resource_id("openai", None, reg)
    assert rid_o == "oa"          # openai 唯一 -> 回退
    rid_d, _, _ = _resolve_resource_id("deepseek", None, reg)
    assert rid_d is None          # deepseek 多个 -> 不猜


def test_8_same_provider_multiple_no_guess(reg_multi):
    rid, err, _ = _resolve_resource_id("deepseek", None, reg_multi)
    assert rid is None and err is None


def test_9_historical_null_unchanged():
    # Phase 3C 不得回填：落库时 resource_id=None 的事件保持 None
    store = EventStore(tempfile.mktemp(suffix=".db"))
    store.insert(AIRequestEvent(provider="deepseek", model="m",
                                resource_id=None, input_tokens=10,
                                output_tokens=5, total_tokens=15,
                                latency_ms=100.0, status_code=200))
    e = store.recent_events(1)[0]
    assert e["resource_id"] is None


def test_10_unattributed_stays_unattributed(reg_multi):
    rid, err, _ = _resolve_resource_id("deepseek", None, reg_multi)
    assert rid is None and err is None


# ============================================================
# A'. Resource Attribution（网关端到端，验证接线）
# ============================================================

def test_e2e_explicit_header_attribution(gw_e2e, monkeypatch):
    m = gw_e2e
    with TestClient(m.app) as client:
        # monkeypatch 必须在 with 内（lifespan init 之后）才不被覆盖
        monkeypatch.setattr(gateway_mod, "resources", ResourceRegistry({
            "ds-only": ResourceDefinition(resource_id="ds-only",
                                          provider="deepseek", enabled=True)}))
        r = client.post("/gateway/deepseek/chat/completions",
                        json={"model": "deepseek-chat", "messages": []},
                        headers={"X-Monitor-Resource": "ds-only"})
        assert r.status_code == 200
        e = m.store.recent_events(1)[0]
        assert e["resource_id"] == "ds-only"


def test_e2e_fallback_unique_attribution(gw_e2e, monkeypatch):
    m = gw_e2e
    with TestClient(m.app) as client:
        monkeypatch.setattr(gateway_mod, "resources", ResourceRegistry({
            "ds-only": ResourceDefinition(resource_id="ds-only",
                                          provider="deepseek", enabled=True)}))
        r = client.post("/gateway/deepseek/chat/completions",
                        json={"model": "deepseek-chat", "messages": []})
        assert r.status_code == 200
        e = m.store.recent_events(1)[0]
        assert e["resource_id"] == "ds-only"


def test_e2e_fallback_multiple_null(gw_e2e, monkeypatch):
    m = gw_e2e
    with TestClient(m.app) as client:
        monkeypatch.setattr(gateway_mod, "resources", ResourceRegistry({
            "ds-a": ResourceDefinition(resource_id="ds-a", provider="deepseek",
                                       enabled=True),
            "ds-b": ResourceDefinition(resource_id="ds-b", provider="deepseek",
                                       enabled=True)}))
        r = client.post("/gateway/deepseek/chat/completions",
                        json={"model": "deepseek-chat", "messages": []})
        assert r.status_code == 200
        e = m.store.recent_events(1)[0]
        assert e["resource_id"] is None


# ============================================================
# B. Resource-level Analytics（复用 Phase 3B efficiency 层）
# ============================================================

def test_11_resource_token_aggregation():
    s = EventStore(tempfile.mktemp(suffix=".db"))
    _seed(s, "R1", n=2, in_t=100, out_t=50)
    rows = {r["name"]: r for r in s.efficiency_by_dim("resource_id")}
    assert rows["R1"]["tokens"]["tokens_per_request"] == 150.0
    assert rows["R1"]["tokens"]["token_coverage"] == 1.0


def test_12_resource_cost_aggregation():
    s = EventStore(tempfile.mktemp(suffix=".db"))
    _seed(s, "R1", n=4, cost=0.01, currency="CNY")
    rows = {r["name"]: r for r in s.efficiency_by_dim("resource_id")}
    c = rows["R1"]["cost_by_currency"]["CNY"]
    assert c["priced_requests"] == 4
    assert c["known_cost"] == pytest.approx(0.04)
    assert c["cost_coverage"] == 1.0


def test_13_resource_error_rate():
    s = EventStore(tempfile.mktemp(suffix=".db"))
    _seed(s, "R1", n=3, status=200)
    _seed(s, "R1", n=1, status=500, error="boom")
    rows = {r["name"]: r for r in s.efficiency_by_dim("resource_id")}
    assert rows["R1"]["requests"] == 4
    assert rows["R1"]["errors"] == 1
    assert rows["R1"]["error_rate"] == pytest.approx(0.25)


def test_14_resource_latency():
    s = EventStore(tempfile.mktemp(suffix=".db"))
    for lat in (100.0, 200.0, 300.0):
        _seed(s, "R1", n=1, latency=lat)
    rows = {r["name"]: r for r in s.efficiency_by_dim("resource_id")}
    assert rows["R1"]["latency"]["p50_latency_ms"] is not None
    assert rows["R1"]["latency"]["p95_latency_ms"] is not None


def test_15_resource_efficiency_present():
    s = EventStore(tempfile.mktemp(suffix=".db"))
    _seed(s, "R1", n=2, cost=0.01)
    rows = {r["name"]: r for r in s.efficiency_by_dim("resource_id")}
    assert "R1" in rows
    assert rows["R1"]["cost_by_currency"]["CNY"]["cost_per_1k_tokens"] is not None


def test_16_null_cost_excluded():
    s = EventStore(tempfile.mktemp(suffix=".db"))
    _seed(s, "R1", n=2, cost=0.01)     # priced
    _seed(s, "R1", n=1, cost=None)     # unknown -> 不计入 priced
    rows = {r["name"]: r for r in s.efficiency_by_dim("resource_id")}
    c = rows["R1"]["cost_by_currency"]["CNY"]
    assert c["priced_requests"] == 2
    assert c["cost_coverage"] == pytest.approx(2 / 3, abs=1e-3)
    assert c["known_cost"] == pytest.approx(0.02)


def test_17_null_token_excluded():
    s = EventStore(tempfile.mktemp(suffix=".db"))
    _seed(s, "R1", n=2, in_t=100, out_t=50)            # tokened
    _seed(s, "R1", n=1, in_t=None, out_t=None, total=None)  # no tokens
    rows = {r["name"]: r for r in s.efficiency_by_dim("resource_id")}
    assert rows["R1"]["tokens"]["tokened_requests"] == 2
    assert rows["R1"]["tokens"]["token_coverage"] == pytest.approx(2 / 3, abs=1e-3)
    assert rows["R1"]["tokens"]["tokens_per_request"] == pytest.approx(150.0)


def test_18_multi_currency_split():
    s = EventStore(tempfile.mktemp(suffix=".db"))
    _seed(s, "R1", n=2, cost=0.01, currency="CNY")
    _seed(s, "R1", n=1, cost=0.02, currency="USD")
    rows = {r["name"]: r for r in s.efficiency_by_dim("resource_id")}
    cb = rows["R1"]["cost_by_currency"]
    assert "CNY" in cb and "USD" in cb
    assert cb["CNY"]["priced_requests"] == 2
    assert cb["USD"]["priced_requests"] == 1


# ============================================================
# C. Health（最小派生）
# ============================================================

def test_19_health_healthy():
    s = EventStore(tempfile.mktemp(suffix=".db"))
    _seed(s, "R1", n=3, status=200)
    h = s.resource_health("R1")
    assert h["health"] == "healthy"
    assert h["requests"] == 3 and h["errors"] == 0
    assert h["error_rate"] == 0.0


def test_20_health_degraded_and_unavailable():
    s = EventStore(tempfile.mktemp(suffix=".db"))
    _seed(s, "R1", n=3, status=200)
    _seed(s, "R1", n=1, status=500, error="x")
    h = s.resource_health("R1")
    assert h["health"] == "degraded"
    assert h["error_rate"] == pytest.approx(0.25)
    # 全部失败 -> unavailable
    s2 = EventStore(tempfile.mktemp(suffix=".db"))
    _seed(s2, "R2", n=2, status=500, error="x")
    assert s2.resource_health("R2")["health"] == "unavailable"


def test_21_health_no_data_unknown():
    s = EventStore(tempfile.mktemp(suffix=".db"))
    h = s.resource_health("R1")
    assert h["health"] == "unknown"
    assert h["error_rate"] is None
    assert h["requests"] == 0


def test_22_health_resource_separation():
    s = EventStore(tempfile.mktemp(suffix=".db"))
    _seed(s, "R1", n=3, status=200)
    _seed(s, "R2", n=2, status=500, error="x")
    assert s.resource_health("R1")["health"] == "healthy"
    assert s.resource_health("R2")["health"] == "unavailable"
