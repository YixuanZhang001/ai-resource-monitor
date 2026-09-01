"""P5-B：Reliability & Monitoring Semantics — 关键修复的 regression 测试。

覆盖：
- D1 落库失败不能中断被代理响应（core.ingest best-effort）。Monitor 是旁路观察者，
     DB 写入失败必须只记录日志、不抛出，被代理的 API 响应照常透传。
- D2 流式连接失败在拿到响应之前 → 记录 502（与 _proxy_once 一致），不静默改写。
- D3 Health 暴露 last_observed_at（新鲜度，非实时探测）；unknown 时该字段为 None。
- D4 透明归因覆盖度 endpoint：registered / unregistered / unattributed 严格分离，
     coverage_pct = registered / total，绝不猜测归属。
"""
import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest
import uvicorn

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402
from monitor.events import AIRequestEvent  # noqa: E402
import monitor.gateway as gw  # noqa: E402
import monitor.main as m  # noqa: E402
from monitor.config import ConfigManager  # noqa: E402
from monitor.resource import ResourceRegistry  # noqa: E402
from monitor.storage import EventStore  # noqa: E402


# --------------------------------------------------------------------------
# 轻量 fixture：直接对 app 发请求（D3/D4，无需上游）
# --------------------------------------------------------------------------

@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(m, "config_mgr", ConfigManager(tmp_path / "config.yaml"))
    monkeypatch.setattr(m, "store", EventStore(tmp_path / "test.db"))
    monkeypatch.setattr(m, "resources", ResourceRegistry(m.config_mgr.resources))
    with TestClient(m.app) as c:
        yield c, m.store


def _ins(store, *, provider="deepseek", model="deepseek-chat",
         input_tokens=10, output_tokens=5, total_tokens=None,
         status_code=200, error=None, timestamp=None, resource_id=None):
    e = AIRequestEvent(
        provider=provider, model=model, input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=total_tokens or (input_tokens + output_tokens),
        latency_ms=100.0, status_code=status_code, error=error,
        resource_id=resource_id)
    if timestamp is not None:
        e.timestamp = timestamp
    store.insert(e)
    return e.request_id


# --------------------------------------------------------------------------
# 真实网关 fixture：mock upstream + 真实 uvicorn（D1/D2，走完整代理链路）
# --------------------------------------------------------------------------

def _make_upstream():
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
            if body.get("stream"):
                sse = ('data: {"choices":[{"delta":{"content":"hi"}}]}\n\n'
                       'data: {"choices":[],"usage":{"prompt_tokens":8,'
                       '"completion_tokens":2,"total_tokens":10}}\n\n'
                       "data: [DONE]\n\n")
                self._send(sse, ctype="text/event-stream")
            else:
                self._send(json.dumps({
                    "id": "chatcmpl-1", "model": body.get("model"),
                    "choices": [{"message": {"role": "assistant",
                                             "content": "ok"}}],
                    "usage": {"prompt_tokens": 10, "completion_tokens": 5,
                              "total_tokens": 15}}))

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


@pytest.fixture()
def gateway_url(tmp_path, monkeypatch):
    upstream = _make_upstream()
    monkeypatch.setattr(m, "config_mgr", ConfigManager(tmp_path / "config.yaml"))
    monkeypatch.setattr(m, "store", EventStore(tmp_path / "test.db"))
    m.config_mgr.upsert("deepseek", enabled=True,
                        base_url=f"http://127.0.0.1:{upstream.server_port}",
                        api_key="sk-test")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")

    config = uvicorn.Config(m.app, host="127.0.0.1", port=0, log_level="warning")
    server = uvicorn.Server(config)
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    for _ in range(200):
        if getattr(server, "started", False) and server.servers:
            break
        time.sleep(0.05)
    port = server.servers[0].sockets[0].getsockname()[1]
    url = f"http://127.0.0.1:{port}"
    yield httpx.Client(base_url=url, timeout=30), m.store
    server.should_exit = True
    t.join(timeout=5)
    upstream.shutdown()


# --------------------------------------------------------------------------
# D1：落库失败不能中断被代理响应
# --------------------------------------------------------------------------

def test_core_ingest_swallows_storage_error():
    """store.insert 抛异常时 ingest 不抛出，仍返回 request_id（best-effort）。"""
    from monitor.core import MonitorCore

    class BoomStore:
        def insert(self, event):
            raise RuntimeError("disk full")

    core = MonitorCore(BoomStore())
    rid = core.ingest({"provider": "openai", "model": "gpt-4o",
                       "status_code": 200, "input_tokens": 1, "output_tokens": 1})
    assert isinstance(rid, str) and len(rid) == 32


def test_gateway_passthrough_survives_ingest_failure(gateway_url, monkeypatch):
    """DB 写入失败时，被代理的上游响应仍 200 且内容不丢。

    关键：修复在 core.ingest 内部（store.insert 包了 try/except），
    因此这里 patch store.insert 触发落库失败，验证 ingest 不会把异常
    上抛到代理链路。
    """
    client, store = gateway_url

    def boom_insert(event):
        raise RuntimeError("db down")

    monkeypatch.setattr(store, "insert", boom_insert)
    r = client.post("/gateway/deepseek/chat/completions",
                    json={"model": "deepseek-chat", "messages": []})
    assert r.status_code == 200
    body = r.json()
    # 上游 content 必须透传（未因监控落库失败而丢失/改写）
    assert body["choices"][0]["message"]["content"] == "ok"


# --------------------------------------------------------------------------
# D2：流式连接失败记录 502
# --------------------------------------------------------------------------

class _RaiseStreamCM:
    """模拟 httpx.stream 在拿到响应之前就抛连接错误的 async 上下文管理器。"""

    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        raise httpx.ConnectError("connection refused")

    async def __aexit__(self, *a):
        return False


def test_stream_connection_failure_records_502(gateway_url, monkeypatch):
    """upstream 连接失败发生在拿到响应之前 → 记录事件 status 502（与 _proxy_once 一致）。"""
    client, store = gateway_url

    def boom_stream(*a, **k):
        return _RaiseStreamCM()

    monkeypatch.setattr(gw.http_client, "stream", boom_stream)
    r = client.post("/gateway/deepseek/chat/completions",
                    json={"model": "deepseek-chat", "stream": True, "messages": []})
    # SSE 连接本身照常建立（200），但后端记录的事件应反映上游连接失败
    assert r.status_code == 200
    evs = store.recent_events(limit=10)
    assert any(e["status_code"] == 502 for e in evs), \
        "stream connection failure must be recorded as 502"


# --------------------------------------------------------------------------
# D3：Health 暴露 last_observed_at
# --------------------------------------------------------------------------

def test_health_includes_last_observed_at(client):
    c, store = client
    c.post("/api/resources", json={"resource_id": "r1", "name": "R1",
                                   "provider": "deepseek", "resource_type": "api",
                                   "billing_mode": "prepaid"})
    now = time.time()
    _ins(store, resource_id="r1", status_code=200, timestamp=now - 100)
    _ins(store, resource_id="r1", status_code=500, error="boom",
         timestamp=now - 10)
    h = c.get("/api/resources/r1/health").json()
    assert h["health"] == "degraded"
    assert h["requests"] == 2
    assert h["errors"] == 1
    assert h["error_rate"] == 0.5
    assert h["last_observed_at"] is not None
    assert abs(h["last_observed_at"] - (now - 10)) < 2


def test_health_unknown_has_none_last_observed(client):
    c, store = client
    h = c.get("/api/resources/nonexistent/health").json()
    assert h["health"] == "unknown"
    assert h["requests"] == 0
    assert h["last_observed_at"] is None


# --------------------------------------------------------------------------
# D4：透明归因覆盖度
# --------------------------------------------------------------------------

def test_attribution_coverage_split(client):
    c, store = client
    c.post("/api/resources", json={"resource_id": "r1", "name": "R1",
                                   "provider": "deepseek", "resource_type": "api",
                                   "billing_mode": "prepaid"})
    c.post("/api/resources", json={"resource_id": "r2", "name": "R2",
                                   "provider": "openai", "resource_type": "api",
                                   "billing_mode": "pay_as_you_go"})
    now = time.time()
    _ins(store, resource_id="r1", timestamp=now)        # registered 1
    _ins(store, resource_id="r1", timestamp=now)        # registered 2
    _ins(store, resource_id=None, timestamp=now)        # unattributed
    _ins(store, resource_id="ghost", timestamp=now)      # unregistered（不在 Registry）

    cov = c.get("/api/attribution/coverage", params={"range": "all"}).json()
    assert cov["total_requests"] == 4
    assert cov["registered_requests"] == 2
    assert cov["unregistered_requests"] == 1
    assert cov["unattributed_requests"] == 1
    assert cov["coverage_pct"] == 50.0
    assert cov["registered_resource_count"] == 2
    assert cov["unregistered_resource_count"] == 1


def test_attribution_coverage_empty(client):
    c, store = client
    cov = c.get("/api/attribution/coverage").json()
    assert cov["total_requests"] == 0
    assert cov["coverage_pct"] is None
    assert cov["registered_requests"] == 0
    assert cov["unattributed_requests"] == 0
    assert cov["unregistered_requests"] == 0
