"""探活探针：GET/HEAD 裸网关路径返回 200 + hint，记 rejected（status 200、无 error），
不污染 ERRORS、不进入 Usage 统计；真实调用路径不受影响。

复刻 test_chain 的 hermetic 夹具（loopback 直连 mock 上游，隔离生产数据）。
"""
import json
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
            self._send(json.dumps({
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
                        api_key="sk-test")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
    with TestClient(m.app) as c:
        yield c, m.store
    upstream.shutdown()


def test_probe_v1_returns_200_and_hint(client):
    c, store = client
    r = c.get("/gateway/deepseek/v1")
    assert r.status_code == 200
    j = r.json()
    assert j["monitor"] is True and j["status"] == "ok"
    assert "chat/completions" in j["hint"]


def test_probe_bare_root_returns_200(client):
    c, store = client
    r = c.get("/gateway/deepseek")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_probe_records_rejected_not_llm_call_and_not_error(client):
    c, store = client
    c.get("/gateway/deepseek/v1")
    c.get("/gateway/deepseek")
    evs = store.recent_events(10)
    assert len(evs) == 2
    for e in evs:
        assert e["event_type"] == "rejected"
        assert e["status_code"] == 200
        assert e["error"] is None
    # overview：探针不计 error、不进 usage
    ov = c.get("/api/stats/overview").json()
    assert ov["errors"] == 0
    assert ov["tokens"] == 0


def test_probe_unknown_provider_still_200(client):
    c, store = client
    r = c.get("/gateway/nope/v1")
    assert r.status_code == 200
    assert r.json()["provider"] == "nope"


def test_real_chat_still_works(client):
    c, store = client
    r = c.post("/gateway/deepseek/chat/completions", json={
        "model": "deepseek-chat", "messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 200
    e = store.recent_events(1)[0]
    assert e["event_type"] == "llm_call" and e["total_tokens"] == 15
