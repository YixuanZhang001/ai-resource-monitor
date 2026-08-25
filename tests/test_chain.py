"""端到端链路测试（本地 mock 上游，不依赖真实 API Key）：

Client → Gateway → Mock Upstream → Response → Monitor Event → SQLite → Stats API

覆盖：OpenAI-compatible（deepseek）与 Gemini 两条协议路径，含流式。
"""
import json
import sqlite3
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient


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

            if self.path.startswith("/gemini/"):
                # Gemini 原生协议
                assert self.headers.get("x-goog-api-key") == "sk-gemini-test"
                meta = {"promptTokenCount": 12, "candidatesTokenCount": 4,
                        "totalTokenCount": 16}
                if body.get("__cache__"):
                    meta["cachedContentTokenCount"] = 40   # 模拟上下文缓存命中
                self._send(json.dumps({
                    "candidates": [{"content": {"parts": [{"text": "ok"}]}}],
                    "usageMetadata": meta,
                }))
                return

            # OpenAI-compatible
            assert self.headers.get("authorization") == "Bearer sk-test"
            if body.get("__error__"):
                # 构造含敏感信息的错误：sk-、ak-（尖括号包裹，JSON 会转义为 \u003c）、
                # Authorization、URL query key
                self._send(json.dumps({
                    "error": {
                        "message": (
                            "invalid key sk-leak-12345678 | "
                            "Authorization: Bearer sk-leak-12345678 | "
                            "<ak-leak-abcdef123> | org-leak-abcdef123 | "
                            "see https://x/v1?key=sk-leak-12345678&token=abc123"
                        )
                    }
                }), status=401)
                return
            if body.get("stream"):
                assert body.get("stream_options", {}).get("include_usage") is True
                sse = (
                    'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n'
                    'data: {"choices":[],"usage":{"prompt_tokens":8,'
                    '"completion_tokens":2,"total_tokens":10}}\n\n'
                    "data: [DONE]\n\n"
                )
                self._send(sse, ctype="text/event-stream")
            else:
                self._send(json.dumps({
                    "id": "chatcmpl-1",
                    "model": body.get("__resp_model") or body.get("model"),
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
    # gateway 模块在 lifespan 里通过 init() 拿到这些实例
    m.config_mgr.upsert("deepseek", enabled=True,
                        base_url=f"http://127.0.0.1:{upstream.server_port}",
                        api_key="sk-test")
    m.config_mgr.upsert("gemini", enabled=True,
                        base_url=f"http://127.0.0.1:{upstream.server_port}/gemini",
                        api_key="sk-gemini-test")
    # 凭据边界：gateway 仅从环境变量经 CredentialProvider 解析 secret（不读 config.yaml）
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
    monkeypatch.setenv("GEMINI_API_KEY", "sk-gemini-test")

    with TestClient(m.app) as c:
        yield c, m.store
    upstream.shutdown()


def test_openai_compat_chain(client):
    c, store = client
    r = c.post("/gateway/deepseek/chat/completions", json={
        "model": "deepseek-chat",
        "messages": [{"role": "user", "content": "hi"}],
    }, headers={"x-monitor-source": "pytest", "x-trace-id": "trace-1"})
    assert r.status_code == 200
    assert r.json()["choices"][0]["message"]["content"] == "ok"

    events = store.recent_events(1)
    e = events[0]
    assert e["provider"] == "deepseek"
    assert e["model"] == "deepseek-chat"
    assert (e["input_tokens"], e["output_tokens"], e["total_tokens"]) == (10, 5, 15)
    assert e["status_code"] == 200 and e["error"] is None
    assert e["cost"] is not None and e["currency"] == "CNY"
    assert e["source"] == "pytest" and e["trace_id"] == "trace-1"
    assert e["latency_ms"] is not None

    # 统计 API 能聚合
    ov = c.get("/api/stats/overview").json()
    assert ov["requests"] == 1 and ov["tokens"] == 15


def test_response_model_overrides_request_alias(client):
    """响应真实 model 覆盖请求 alias（Step 5 model identity）。"""
    c, store = client
    c.post("/gateway/deepseek/chat/completions", json={
        "model": "deepseek-chat",              # 请求 alias
        "__resp_model": "deepseek-v4-flash",    # 响应真实 model
        "messages": [{"role": "user", "content": "hi"}],
    })
    e = store.recent_events(1)[0]
    assert e["model"] == "deepseek-v4-flash"   # 用响应真实 model，非请求 alias
    # v4-flash 已校准（2026-08-17 官方价）→ cost 非 None
    assert e["cost"] is not None and e["currency"] == "CNY"


def test_cache_hit_three_states(client):
    """cache_hit 三态：None=未知（adapter 不提取），0=明确未命中，1=命中。"""
    c, store = client
    # OpenAI-compat mock 无 prompt_cache_hit_tokens → cache_read None → cache_hit None（未知）
    c.post("/gateway/deepseek/chat/completions",
           json={"model": "deepseek-chat", "messages": []})
    e = store.recent_events(1)[0]
    assert e["cache_read_tokens"] is None
    assert e["cache_hit"] is None              # 未知，非 0


def test_openai_compat_stream_chain(client):
    c, store = client
    r = c.post("/gateway/deepseek/chat/completions", json={
        "model": "deepseek-chat", "stream": True,
        "messages": [{"role": "user", "content": "hi"}],
    })
    assert r.status_code == 200
    assert "data:" in r.text

    e = store.recent_events(1)[0]
    assert (e["input_tokens"], e["total_tokens"]) == (8, 10)


def test_gemini_chain(client):
    c, store = client
    r = c.post("/gateway/gemini/v1beta/models/gemini-2.5-flash:generateContent",
               json={"contents": [{"parts": [{"text": "hi"}]}]})
    assert r.status_code == 200

    e = store.recent_events(1)[0]
    assert e["provider"] == "gemini"
    assert e["model"] == "gemini-2.5-flash"
    assert (e["input_tokens"], e["output_tokens"], e["total_tokens"]) == (12, 4, 16)
    assert e["currency"] == "USD"
    # 未使用缓存：cache 字段为 NULL（不伪造）
    assert e["cache_read_tokens"] is None and e["cache_hit"] is None


def test_gemini_cache_chain(client):
    """Gemini 上游返回 cachedContentTokenCount → 事件 cache_read_tokens 落库。"""
    c, store = client
    r = c.post("/gateway/gemini/v1beta/models/gemini-2.5-flash:generateContent",
               json={"contents": [{"parts": [{"text": "hi"}]}], "__cache__": True})
    assert r.status_code == 200
    e = store.recent_events(1)[0]
    assert e["cache_read_tokens"] == 40
    assert e["cache_hit"] == 1


def test_openai_compat_cache_is_null(client):
    """OpenAI-compatible 上游无 cache 指标 → 事件 cache 为 NULL。"""
    c, store = client
    c.post("/gateway/deepseek/chat/completions",
           json={"model": "deepseek-chat", "messages": []})
    e = store.recent_events(1)[0]
    assert e["cache_read_tokens"] is None
    assert e["cache_hit"] is None


def test_disabled_provider_rejected(client):
    c, _ = client
    r = c.post("/gateway/kimi/chat/completions", json={"model": "kimi-k2"})
    assert r.status_code == 400


def test_unknown_provider_rejected(client):
    c, _ = client
    r = c.post("/gateway/notexist/chat/completions", json={})
    assert r.status_code == 404


def test_api_key_never_leaks(client):
    c, store = client
    c.post("/gateway/deepseek/chat/completions",
           json={"model": "deepseek-chat", "messages": []})
    # 配置 API 不回显 key
    for p in c.get("/api/providers").json()["providers"]:
        assert "api_key" not in p
    # 数据库不含 key 明文
    db_path = store.db_path
    raw = sqlite3.connect(db_path).execute(
        "SELECT sql FROM sqlite_master").fetchall()
    assert all("api_key" not in str(s).lower() for s in raw)
    row = sqlite3.connect(db_path).execute(
        "SELECT * FROM events LIMIT 1").fetchone()
    assert all("sk-test" not in str(v) for v in row)


def test_error_sanitized_everywhere(client):
    """上游错误含 sk-/ak-/org-/Authorization/URL query key 时，
    客户端响应、事件、SQLite、Dashboard API 四处的 error 都必须已脱敏。"""
    import json as _json
    c, store = client
    r = c.post("/gateway/deepseek/chat/completions", json={
        "model": "deepseek-chat", "__error__": True,
        "messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 401

    # 1) 客户端响应已脱敏
    assert "sk-leak-12345678" not in r.text
    assert "ak-leak-abcdef123" not in r.text
    assert "org-leak-abcdef123" not in r.text
    assert "[REDACTED]" in r.text

    # 2) 事件模型 / SQLite 已脱敏
    e = store.recent_events(1)[0]
    assert e["status_code"] == 401 and e["error"] is not None
    for frag in ("sk-leak-12345678", "ak-leak-abcdef123",
                 "org-leak-abcdef123", "abc123"):
        assert frag not in e["error"]
    assert "[REDACTED]" in e["error"]

    raw = sqlite3.connect(store.db_path).execute(
        "SELECT error FROM events ORDER BY id DESC LIMIT 1").fetchone()[0]
    assert "sk-leak-12345678" not in raw
    assert "[REDACTED]" in raw

    # 3) Dashboard 数据源（/api/events）已脱敏
    body = _json.dumps(c.get("/api/events?limit=5").json())
    assert "sk-leak-12345678" not in body
    assert "ak-leak-abcdef123" not in body


# ---------- P0-2：Resource Attribution ----------

def test_explicit_resource_attribution(client, monkeypatch):
    """X-Monitor-Resource 存在 → event.resource_id 落库。"""
    c, store = client
    import monitor.gateway as gw
    from monitor.resource import ResourceDefinition, ResourceRegistry
    monkeypatch.setattr(gw, "resources", ResourceRegistry({
        "deepseek-paid": ResourceDefinition(
            resource_id="deepseek-paid", provider="deepseek")}))
    r = c.post("/gateway/deepseek/chat/completions",
               json={"model": "deepseek-chat", "messages": []},
               headers={"X-Monitor-Resource": "deepseek-paid"})
    assert r.status_code == 200
    e = store.recent_events(1)[0]
    assert e["resource_id"] == "deepseek-paid"   # 显式归因落库


def test_no_header_resource_null(client):
    """无 X-Monitor-Resource → resource_id NULL，请求正常。"""
    c, store = client
    c.post("/gateway/deepseek/chat/completions",
           json={"model": "deepseek-chat", "messages": []})
    e = store.recent_events(1)[0]
    assert e["resource_id"] is None
    assert e["cost"] is not None  # pricing 不受影响


def test_unknown_resource_rejected(client, monkeypatch):
    """X-Monitor-Resource 未注册 → 400 拒绝，不自动创建。"""
    c, store = client
    # fixture 未配置 resources → 任何 header 都未知
    r = c.post("/gateway/deepseek/chat/completions",
               json={"model": "deepseek-chat", "messages": []},
               headers={"X-Monitor-Resource": "does-not-exist"})
    assert r.status_code == 400
    assert "unknown resource" in r.text
    # 不创建 Event（请求被拒绝在落库前）：store 仍为空
    assert store.recent_events(5) == []
