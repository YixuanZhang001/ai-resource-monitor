"""第二阶段 Analytics 测试：
Source/Project 记录与 Unknown 回退、维度聚合、Cost、Tokens、Performance、
Requests 分页/筛选/详情、旧数据（NULL）兼容。
"""
import json
import sqlite3
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402

from monitor.events import AIRequestEvent  # noqa: E402


def make_upstream():
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, body, status=200, ctype="application/json"):
            data = body.encode()
            self.send_response(status)
            self.send_header("content-type", ctype)
            self.send_header("content-length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_POST(self):
            length = int(self.headers.get("content-length") or 0)
            body = json.loads(self.rfile.read(length) or b"{}")
            assert self.headers.get("authorization") == "Bearer sk-test"
            if body.get("__error__"):
                self._send(json.dumps({"error": {"message": "boom"}}), status=401)
                return
            self._send(json.dumps({
                "id": "chatcmpl-1", "model": body.get("model"),
                "choices": [{"message": {"role": "assistant", "content": "ok"}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5,
                          "total_tokens": 15},
            }))

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


@pytest.fixture()
def client(tmp_path, monkeypatch):
    upstream = make_upstream()

    import monitor.main as m
    from monitor.config import ConfigManager
    from monitor.storage import EventStore

    monkeypatch.setattr(m, "config_mgr", ConfigManager(tmp_path / "config.yaml"))
    monkeypatch.setattr(m, "store", EventStore(tmp_path / "test.db"))
    m.config_mgr.upsert("deepseek", enabled=True,
                        base_url=f"http://127.0.0.1:{upstream.server_port}",
                        api_key="sk-test", test_model="deepseek-chat")
    # 凭据边界：gateway 仅从环境变量经 CredentialProvider 解析 secret（不读 config.yaml）
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
    m.config_mgr.set_dim_values(sources=["VS Code", "PAL"],
                                projects=["PAL", "Research"])

    with TestClient(m.app) as c:
        yield c, m.store
    upstream.shutdown()


def insert_event(store, *, provider="deepseek", model="deepseek-chat",
                 source=None, project=None, input_tokens=100, output_tokens=50,
                 total_tokens=None, latency_ms=100.0, status_code=200,
                 cost=None, currency=None, error=None, timestamp=None,
                 request_id=None):
    e = AIRequestEvent(
        provider=provider, model=model, source=source, project=project,
        input_tokens=input_tokens, output_tokens=output_tokens,
        total_tokens=total_tokens or (input_tokens + output_tokens),
        latency_ms=latency_ms, status_code=status_code,
        cost=cost, currency=currency, error=error,
    )
    if timestamp is not None:
        e.timestamp = timestamp
    if request_id:
        e.request_id = request_id
    store.insert(e)
    return e.request_id


# ---------- Source / Project ----------

def test_source_project_header_recorded(client):
    c, store = client
    c.post("/gateway/deepseek/chat/completions", json={
        "model": "deepseek-chat", "messages": [{"role": "user", "content": "hi"}]},
        headers={"X-Monitor-Source": "VS Code", "X-Monitor-Project": "PAL"})
    e = store.recent_events(1)[0]
    assert e["source"] == "VS Code" and e["project"] == "PAL"


def test_source_project_default_unknown(client):
    """缺失 header → 落库为 NULL，但展示层统一回退 Unknown（不改历史数据）。"""
    c, store = client
    rid = insert_event(store, source=None, project=None)
    # 列表与详情均回退为 Unknown
    items = c.get("/api/requests").json()["items"]
    assert items[0]["source"] == "Unknown"
    assert items[0]["project"] == "Unknown"
    detail = c.get(f"/api/requests/{rid}").json()
    assert detail["source"] == "Unknown" and detail["project"] == "Unknown"
    # 原始库中仍是 NULL（未篡改历史数据）
    conn = sqlite3.connect(store.db_path)
    row = conn.execute("SELECT source, project FROM events WHERE request_id=?",
                       (rid,)).fetchone()
    conn.close()
    assert row == (None, None)


def test_dim_values_api(client):
    c, store = client
    insert_event(store, source="VS Code", project="PAL")
    insert_event(store, source=None, project=None)
    src = c.get("/api/sources").json()
    assert "VS Code" in src["sources"] and "Unknown" in src["sources"]
    prj = c.get("/api/projects").json()
    assert "PAL" in prj["projects"] and "Unknown" in prj["projects"]


# ---------- Analytics 聚合 ----------

def _seed(store):
    """构造跨 provider/model/source/project/status 的数据。"""
    now = time.time()
    insert_event(store, provider="deepseek", model="deepseek-chat",
                 source="VS Code", project="PAL", input_tokens=1000,
                 output_tokens=500, latency_ms=100, status_code=200,
                 cost=0.002, currency="CNY", timestamp=now)
    insert_event(store, provider="deepseek", model="deepseek-chat",
                 source="PAL", project="PAL", input_tokens=2000,
                 output_tokens=1000, latency_ms=200, status_code=200,
                 cost=0.004, currency="CNY", timestamp=now)
    insert_event(store, provider="qwen", model="qwen-plus",
                 source="PAL", project="Research", input_tokens=500,
                 output_tokens=250, latency_ms=50, status_code=200,
                 cost=0.001, currency="CNY", timestamp=now)
    insert_event(store, provider="deepseek", model="deepseek-chat",
                 source="VS Code", project="PAL", input_tokens=100,
                 output_tokens=50, latency_ms=900, status_code=500,
                 error="boom", timestamp=now)
    insert_event(store, provider="kimi", model="kimi-k2-0905-preview",
                 source=None, project=None, input_tokens=10,
                 output_tokens=5, latency_ms=30, status_code=401,
                 error="bad key", timestamp=now - 10 * 86400)  # 10 天前


def test_overview_aggregation(client):
    c, store = client
    _seed(store)
    ov = c.get("/api/overview").json()
    assert ov["requests"] == 5
    # 总 token：1500 + 3000 + 150 + 750 + 15
    assert ov["total_tokens"] == 5415
    assert ov["errors"] == 2
    assert round(ov["error_rate"], 3) == 0.4
    assert ov["cost_by_currency"]["CNY"] == 0.007


def test_range_filter(client):
    c, store = client
    _seed(store)
    ov_today = c.get("/api/overview", params={"range": "today"}).json()
    assert ov_today["requests"] == 4          # 排除 10 天前那条
    ov_7d = c.get("/api/overview", params={"range": "7d"}).json()
    assert ov_7d["requests"] == 4
    ov_all = c.get("/api/overview", params={"range": "all"}).json()
    assert ov_all["requests"] == 5


def test_cost_by_dimension(client):
    c, store = client
    _seed(store)
    by_provider = c.get("/api/analytics/cost",
                        params={"dim": "provider"}).json()["rows"]
    d = {r["name"]: r["cost"] for r in by_provider}
    assert d["deepseek"] == 0.006
    assert d["qwen"] == 0.001
    assert "kimi" not in d  # kimi 无 cost 不计入
    by_model = c.get("/api/analytics/cost",
                     params={"dim": "model"}).json()["rows"]
    assert by_model[0]["name"] == "deepseek-chat"
    by_source = c.get("/api/analytics/cost",
                      params={"dim": "source"}).json()["rows"]
    assert {r["name"]: r["cost"] for r in by_source}["VS Code"] == 0.002
    by_project = c.get("/api/analytics/cost",
                       params={"dim": "project"}).json()["rows"]
    assert {r["name"]: r["cost"] for r in by_project}["PAL"] == 0.006


def test_tokens_by_dimension(client):
    c, store = client
    _seed(store)
    rows = c.get("/api/analytics/tokens",
                 params={"dim": "provider"}).json()["rows"]
    d = {r["name"]: r for r in rows}
    assert d["deepseek"]["input_tokens"] == 3100
    assert d["deepseek"]["output_tokens"] == 1550
    assert d["deepseek"]["total_tokens"] == 4650
    assert d["qwen"]["total_tokens"] == 750


def test_tokens_timeseries(client):
    c, store = client
    _seed(store)
    rows = c.get("/api/analytics/tokens/timeseries",
                 params={"days": 30}).json()["rows"]
    assert len(rows) >= 1
    assert rows[0]["total_tokens"] > 0
    assert "input_tokens" in rows[0] and "output_tokens" in rows[0]


def test_performance_stats(client):
    c, store = client
    _seed(store)
    perf = c.get("/api/analytics/performance").json()
    assert perf["request_count"] == 5
    assert perf["avg_latency_ms"] == 256.0  # (100+200+50+900+30)/5
    assert perf["p50_latency_ms"] == 100.0
    assert perf["p95_latency_ms"] == 900.0
    assert round(perf["error_rate"], 3) == 0.4


def test_performance_empty_is_null(client):
    c, _ = client
    perf = c.get("/api/analytics/performance").json()
    assert perf["request_count"] == 0
    assert perf["p50_latency_ms"] is None
    assert perf["p95_latency_ms"] is None
    assert perf["error_rate"] is None


def test_requests_pagination(client):
    c, store = client
    for i in range(25):
        insert_event(store, provider="deepseek",
                     model=f"m{i % 3}", source="PAL", project="P",
                     latency_ms=float(i), status_code=200)
    r1 = c.get("/api/requests", params={"page": 1, "page_size": 10}).json()
    assert r1["total"] == 25 and r1["pages"] == 3
    assert len(r1["items"]) == 10
    r3 = c.get("/api/requests", params={"page": 3, "page_size": 10}).json()
    assert len(r3["items"]) == 5


def test_requests_filters(client):
    c, store = client
    insert_event(store, provider="deepseek", model="deepseek-chat",
                 source="VS Code", project="PAL", status_code=200)
    insert_event(store, provider="qwen", model="qwen-plus",
                 source="PAL", project="Research", status_code=500, error="x")
    f1 = c.get("/api/requests",
               params={"provider": "qwen"}).json()
    assert f1["total"] == 1 and f1["items"][0]["model"] == "qwen-plus"
    f2 = c.get("/api/requests", params={"status": 200}).json()
    assert f2["total"] == 1
    f3 = c.get("/api/requests", params={"source": "VS Code"}).json()
    assert f3["total"] == 1
    f4 = c.get("/api/requests", params={"project": "Research"}).json()
    assert f4["total"] == 1


def test_request_detail_and_404(client):
    c, store = client
    rid = insert_event(store, provider="deepseek", model="deepseek-chat",
                       source="VS Code", project="PAL", input_tokens=100,
                       output_tokens=50, total_tokens=150, latency_ms=123.4,
                       status_code=200, cost=0.001, currency="CNY",
                       error=None, request_id="req-detail-001")
    d = c.get("/api/requests/req-detail-001").json()
    assert d["provider"] == "deepseek"
    assert d["source"] == "VS Code"
    assert d["total_tokens"] == 150
    assert d["latency_ms"] == 123.4
    assert d["cost"] == 0.001
    assert c.get("/api/requests/nonexistent-id").status_code == 404


def test_analytics_bad_dim_rejected(client):
    c, _ = client
    assert c.get("/api/analytics/cost",
                 params={"dim": "hack"}).status_code == 400
    assert c.get("/api/analytics/tokens",
                 params={"dim": "hack"}).status_code == 400


def test_dim_values_configured(client):
    """Settings 的 Source/Project 配置持久化并可读回。"""
    c, store = client
    r = c.put("/api/dim-values",
              json={"sources": ["Cursor", "Claude Code"],
                    "projects": ["My App"]}).json()
    assert r["sources"] == ["Cursor", "Claude Code"]
    assert r["projects"] == ["My App"]
    assert c.get("/api/sources").json()["configured"] == ["Cursor", "Claude Code"]
    assert c.get("/api/projects").json()["configured"] == ["My App"]
    # 持久化到文件
    from monitor.main import config_mgr
    assert config_mgr.sources == ["Cursor", "Claude Code"]
