"""PH3 测试：SSE Live Request Stream（真实 uvicorn + 同步 httpx + threading）。

用同步 httpx.Client 消费 SSE 长连接 + threading.Event 握手，避免 pytest 进程内
asyncio.run 与 uvicorn 子线程 loop 的不兼容。仍走真实网络链路（uvicorn +
mock upstream），不 mock 产品 SSE 逻辑，不调用真实 Provider API。
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
            if body.get("__error__"):
                self._send(json.dumps({"error": {"message": "boom"}}), status=401)
                return
            if body.get("stream"):
                sse = (
                    'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n'
                    'data: {"choices":[],"usage":{"prompt_tokens":8,'
                    '"completion_tokens":2,"total_tokens":10}}\n\n'
                    "data: [DONE]\n\n"
                )
                self._send(sse, ctype="text/event-stream")
            else:
                self._send(json.dumps({
                    "id": "chatcmpl-1", "model": body.get("model"),
                    "choices": [{"message": {"role": "assistant",
                                             "content": "ok"}}],
                    "usage": {"prompt_tokens": 10, "completion_tokens": 5,
                              "total_tokens": 15},
                }))

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


@pytest.fixture()
def base_url(tmp_path, monkeypatch):
    upstream = make_upstream()
    import monitor.main as m
    from monitor.config import ConfigManager
    from monitor.storage import EventStore

    monkeypatch.setattr(m, "config_mgr", ConfigManager(tmp_path / "config.yaml"))
    monkeypatch.setattr(m, "store", EventStore(tmp_path / "test.db"))
    m.config_mgr.upsert("deepseek", enabled=True,
                        base_url=f"http://127.0.0.1:{upstream.server_port}",
                        api_key="sk-test")
    # 凭据边界：gateway 仅从环境变量经 CredentialProvider 解析 secret（不读 config.yaml）
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
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    t.join(timeout=5)
    upstream.shutdown()


def _subscribe(client, ready: threading.Event, msgs: list, need: int):
    """在子线程消费 SSE；ready 在握手后 set；收到 need 条后退出。"""

    def read():
        try:
            with client.stream("GET", "/api/requests/stream") as r:
                assert r.status_code == 200
                assert r.headers["content-type"].startswith("text/event-stream")
                for line in r.iter_lines():
                    if line.startswith(": connected"):
                        ready.set()
                    elif line.startswith("data:"):
                        p = line[5:].strip()
                        if p and p != "[DONE]":
                            msgs.append(json.loads(p))
                            if len(msgs) >= need:
                                return
        except Exception:
            return

    t = threading.Thread(target=read, daemon=True)
    t.start()
    return t


def _fire(client, ready, **payload):
    assert ready.wait(timeout=5), "SSE 订阅握手超时"
    return client.post("/gateway/deepseek/chat/completions", json=payload)


def _new_client(base_url):
    return httpx.Client(base_url=base_url, timeout=30)


def test_sse_connection_and_event(base_url):
    with _new_client(base_url) as c:
        ready = threading.Event()
        msgs = []
        t = _subscribe(c, ready, msgs, need=1)
        _fire(c, ready, model="deepseek-chat", messages=[])
        t.join(timeout=8)
        assert len(msgs) == 1
        m = msgs[0]
        assert m["type"] == "done"
        ev = m["event"]
        assert ev["provider"] == "deepseek"
        assert ev["model"] == "deepseek-chat"
        assert ev["total_tokens"] == 15
        assert ev["status_code"] == 200
        assert ev["cache_read_tokens"] is None
        blob = json.dumps(m)
        assert "sk-" not in blob
        assert "authorization" not in blob.lower()
        assert "messages" not in blob and "choices" not in blob


def test_sse_stream_begin_then_done(base_url):
    with _new_client(base_url) as c:
        ready = threading.Event()
        msgs = []
        t = _subscribe(c, ready, msgs, need=2)
        _fire(c, ready, model="deepseek-chat", stream=True, messages=[])
        t.join(timeout=10)
        assert [m["type"] for m in msgs] == ["begin", "done"]
        assert msgs[1]["event"]["total_tokens"] == 10


def test_sse_error_event(base_url):
    with _new_client(base_url) as c:
        ready = threading.Event()
        msgs = []
        t = _subscribe(c, ready, msgs, need=1)
        _fire(c, ready, model="deepseek-chat", __error__=True, messages=[])
        t.join(timeout=8)
        assert msgs[0]["type"] == "error"
        assert msgs[0]["event"]["status_code"] == 401
        assert msgs[0]["event"]["error"] is not None


def test_sse_multiple_subscribers(base_url):
    with _new_client(base_url) as c1, _new_client(base_url) as c2:
        r1, r2 = threading.Event(), threading.Event()
        m1, m2 = [], []
        t1 = _subscribe(c1, r1, m1, need=1)
        t2 = _subscribe(c2, r2, m2, need=1)
        _fire(c1, r1, model="deepseek-chat", messages=[])
        _fire(c2, r2, model="deepseek-chat", messages=[])
        t1.join(timeout=8)
        t2.join(timeout=8)
        assert m1 and m2
        assert m1[0]["event"]["request_id"] == m2[0]["event"]["request_id"]


def test_sse_disconnect_does_not_block(base_url):
    with _new_client(base_url) as c:
        with c.stream("GET", "/api/requests/stream"):
            pass  # 立即断开
        for _ in range(3):
            resp = c.post("/gateway/deepseek/chat/completions",
                          json={"model": "deepseek-chat", "messages": []})
            assert resp.status_code == 200


def test_sse_no_subscriber_no_block(base_url):
    with _new_client(base_url) as c:
        for _ in range(3):
            resp = c.post("/gateway/deepseek/chat/completions",
                          json={"model": "deepseek-chat", "messages": []})
            assert resp.status_code == 200


def test_sse_non_stream_no_begin(base_url):
    with _new_client(base_url) as c:
        ready = threading.Event()
        msgs = []
        t = _subscribe(c, ready, msgs, need=2)
        _fire(c, ready, model="deepseek-chat", messages=[])
        t.join(timeout=3)   # 非流式只 1 条，need=2 不会满足 → 3s 后超时退出
        assert len(msgs) == 1
        assert msgs[0]["type"] == "done"
