"""P0-5 测试：Resource Management（CRUD API + 持久化 + 安全 + 事件兼容 + runtime）。

覆盖任务要求：registry 8 项、persistence 2、security 3、event compat 3、
runtime 4、dashboard 数据 5 项。
"""
import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402

from monitor.config import ConfigManager  # noqa: E402
from monitor.events import AIRequestEvent  # noqa: E402
from monitor.resource import ResourceRegistry  # noqa: E402
from monitor.storage import EventStore  # noqa: E402

CFG = (
    "scheduler: {enabled: false}\n"
    "providers:\n"
    "  deepseek:\n    enabled: true\n    base_url: http://up\n"
    "    api_keys:\n    - sk-test\n"
)


def _insert(store, *, provider="deepseek", model="deepseek-v4-flash",
            resource_id=None, tokens=10, cost=0.001):
    store.insert(AIRequestEvent(
        provider=provider, model=model, resource_id=resource_id,
        input_tokens=tokens, output_tokens=5, total_tokens=tokens + 5,
        latency_ms=100.0, status_code=200, estimated_cost=cost,
        timestamp=time.time()))


@pytest.fixture()
def app(tmp_path, monkeypatch):
    """生产等价：config_mgr 与 resources 共享同一 dict（lifespan 重建逻辑）。"""
    import monitor.main as m

    cfg_path = tmp_path / "c.yaml"
    cfg_path.write_text(CFG, encoding="utf-8")
    monkeypatch.setattr(m, "config_mgr", ConfigManager(cfg_path))
    monkeypatch.setattr(m, "store", EventStore(tmp_path / "t.db"))
    monkeypatch.setattr(m, "resources", ResourceRegistry(m.config_mgr.resources))
    with TestClient(m.app) as c:
        yield c, m


# ---------- Registry / API CRUD ----------

def test_create_resource(app):
    c, m = app
    r = c.post("/api/resources", json={
        "resource_id": "kimi-paid", "name": "Kimi Paid",
        "provider": "kimi", "resource_type": "api",
        "billing_mode": "prepaid", "account_scope": "acct-1"})
    assert r.status_code == 200
    d = r.json()
    assert d["resource_id"] == "kimi-paid" and d["enabled"] is True
    # 立即可见（无需重启）
    assert c.get("/api/resources").json()["resources"][0]["resource_id"] == "kimi-paid"
    # 立即可归因（runtime）：X-Monitor-Resource 通过校验
    assert m.resources.exists("kimi-paid")


def test_duplicate_resource_rejected(app):
    c, m = app
    body = {"resource_id": "dup", "name": "D", "provider": "deepseek"}
    assert c.post("/api/resources", json=body).status_code == 200
    assert c.post("/api/resources", json=body).status_code == 409


def test_get_resource(app):
    c, m = app
    c.post("/api/resources", json={"resource_id": "r1", "name": "R1",
                                   "provider": "deepseek"})
    d = c.get("/api/resources/r1").json()
    assert d["resource_id"] == "r1" and d["name"] == "R1"
    assert c.get("/api/resources/nope").status_code == 404


def test_update_resource(app):
    c, m = app
    c.post("/api/resources", json={"resource_id": "r1", "name": "R1",
                                   "provider": "deepseek", "enabled": True})
    r = c.put("/api/resources/r1", json={
        "name": "R1 New", "provider": "deepseek",
        "resource_type": "quota", "billing_mode": "free", "enabled": False})
    assert r.status_code == 200
    d = r.json()
    assert d["name"] == "R1 New" and d["enabled"] is False
    assert d["resource_type"] == "quota"          # 更新生效
    assert d["resource_id"] == "r1"               # id 不可变


def test_enable_disable(app):
    c, m = app
    c.post("/api/resources", json={"resource_id": "r1", "name": "R1",
                                   "provider": "deepseek"})
    assert c.put("/api/resources/r1", json={
        "name": "R1", "provider": "deepseek", "enabled": False}).json()["enabled"] is False
    assert c.get("/api/resources/r1").json()["enabled"] is False
    assert c.put("/api/resources/r1", json={
        "name": "R1", "provider": "deepseek", "enabled": True}).json()["enabled"] is True


def test_delete_resource(app):
    c, m = app
    c.post("/api/resources", json={"resource_id": "r1", "name": "R1",
                                   "provider": "deepseek"})
    r = c.delete("/api/resources/r1")
    assert r.status_code == 200
    assert r.json()["historical_events_preserved"] is True
    assert c.get("/api/resources/r1").status_code == 404
    assert c.delete("/api/resources/r1").status_code == 404   # 二次删除 404


def test_invalid_resource_type_rejected(app):
    c, m = app
    r = c.post("/api/resources", json={"resource_id": "r1", "name": "R1",
                                       "provider": "deepseek",
                                       "resource_type": "quantum"})
    assert r.status_code == 422


def test_invalid_billing_mode_rejected(app):
    c, m = app
    r = c.post("/api/resources", json={"resource_id": "r1", "name": "R1",
                                       "provider": "deepseek",
                                       "billing_mode": "free99"})
    assert r.status_code == 422


def test_invalid_resource_id_rejected(app):
    c, m = app
    assert c.post("/api/resources", json={
        "resource_id": "bad id!", "name": "R", "provider": "deepseek"}
    ).status_code == 422
    assert c.post("/api/resources", json={
        "resource_id": "", "name": "R", "provider": "deepseek"}
    ).status_code == 400


# ---------- Persistence ----------

def test_create_survives_reload(app, tmp_path):
    c, m = app
    c.post("/api/resources", json={"resource_id": "persist-1", "name": "P1",
                                   "provider": "deepseek",
                                   "resource_type": "quota",
                                   "billing_mode": "free"})
    # 用同一文件重新加载（模拟重启）
    cm2 = ConfigManager(tmp_path / "c.yaml")
    assert "persist-1" in cm2.resources
    assert cm2.resources["persist-1"].billing_mode == "free"


def test_update_survives_reload(app, tmp_path):
    c, m = app
    c.post("/api/resources", json={"resource_id": "p2", "name": "P2",
                                   "provider": "deepseek"})
    c.put("/api/resources/p2", json={"name": "P2 Renamed",
                                     "provider": "deepseek", "enabled": False})
    cm2 = ConfigManager(tmp_path / "c.yaml")
    assert cm2.resources["p2"].name == "P2 Renamed"
    assert cm2.resources["p2"].enabled is False


# ---------- Security ----------

def test_secret_in_account_scope_rejected(app):
    c, m = app
    r = c.post("/api/resources", json={"resource_id": "r1", "name": "R1",
                                       "provider": "deepseek",
                                       "account_scope": "sk-abcdef123"})
    assert r.status_code == 422
    r2 = c.post("/api/resources", json={"resource_id": "r1", "name": "R1",
                                        "provider": "deepseek",
                                        "account_scope": "acct with space"})
    assert r2.status_code == 422


def test_secret_in_metadata_rejected(app):
    c, m = app
    r = c.post("/api/resources", json={"resource_id": "r1", "name": "R1",
                                       "provider": "deepseek",
                                       "metadata": {"api_key": "sk-leak"}})
    assert r.status_code == 422
    r2 = c.post("/api/resources", json={"resource_id": "r1", "name": "R1",
                                        "provider": "deepseek",
                                        "metadata": {"note": "token sk-leak"}})
    assert r2.status_code == 422


def test_api_never_exposes_credentials(app):
    c, m = app
    c.post("/api/resources", json={"resource_id": "r1", "name": "R1",
                                   "provider": "deepseek",
                                   "account_scope": "acct-a"})
    for path in ("/api/resources", "/api/resources/r1"):
        body = c.get(path).text
        assert "sk-" not in body and "bearer" not in body.lower()


# ---------- Event compatibility ----------

def test_disable_keeps_historical_events(app):
    c, m = app
    _insert(m.store, resource_id="keep-res")
    c.post("/api/resources", json={"resource_id": "keep-res", "name": "K",
                                   "provider": "deepseek"})
    c.put("/api/resources/keep-res", json={
        "name": "K", "provider": "deepseek", "enabled": False})
    e = m.store.recent_events(1)[0]
    assert e["resource_id"] == "keep-res"     # 事件不变


def test_delete_preserves_events_as_unregistered(app):
    c, m = app
    _insert(m.store, resource_id="ghost-res", tokens=10, cost=0.001)
    c.post("/api/resources", json={"resource_id": "ghost-res", "name": "G",
                                   "provider": "deepseek"})
    c.delete("/api/resources/ghost-res")
    # 历史事件保留
    e = m.store.recent_events(1)[0]
    assert e["resource_id"] == "ghost-res"
    # usage API：ghost-res 进入 unregistered（不是 unattributed，不是资源列表）
    d = c.get("/api/resources/usage").json()
    assert [r for r in d["resources"] if r["resource_id"] == "ghost-res"] == []
    unreg = [r for r in d["unregistered"] if r["resource_id"] == "ghost-res"]
    assert len(unreg) == 1 and unreg[0]["requests"] == 1
    assert unreg[0]["registered"] is False
    assert d["unattributed"]["requests"] == 0   # 未伪装成 unattributed
    # 详情：孤儿 id 返回 registered:false（非 404）
    det = c.get("/api/resources/ghost-res/usage").json()
    assert det["registered"] is False and det["resource"]["requests"] == 1
    # 真正未知（无事件无注册）→ 404
    assert c.get("/api/resources/nope/usage").status_code == 404


def test_null_resource_id_stays_unattributed(app):
    c, m = app
    _insert(m.store, resource_id=None, tokens=7)
    d = c.get("/api/resources/usage").json()
    assert d["unattributed"]["requests"] == 1


# ---------- Runtime（gateway 归因） ----------

def make_upstream():
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            length = int(self.headers.get("content-length") or 0)
            body = json.loads(self.rfile.read(length) or b"{}")
            data = json.dumps({
                "id": "chatcmpl-1", "model": body.get("model") or "deepseek-v4-flash",
                "choices": [{"message": {"role": "assistant", "content": "ok"}}],
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
def gw(app, tmp_path, monkeypatch):
    """带 mock upstream 的网关环境。"""
    upstream = make_upstream()
    import monitor.main as m
    m.config_mgr.upsert("deepseek", enabled=True,
                        base_url=f"http://127.0.0.1:{upstream.server_port}",
                        api_key="sk-test")
    yield m
    upstream.shutdown()


def test_created_resource_immediately_attributable(gw):
    m = gw
    c = TestClient.__new__(object)  # placeholder not used
    with TestClient(m.app) as client:
        # 创建资源后立即可用于 X-Monitor-Resource
        r = client.post("/api/resources", json={
            "resource_id": "fresh-res", "name": "Fresh",
            "provider": "deepseek"})
        assert r.status_code == 200
        r2 = client.post("/gateway/deepseek/chat/completions",
                         json={"model": "deepseek-chat", "messages": []},
                         headers={"X-Monitor-Resource": "fresh-res"})
        assert r2.status_code == 200
        e = m.store.recent_events(1)[0]
        assert e["resource_id"] == "fresh-res"


def test_disabled_resource_attribution_rejected(gw):
    m = gw
    with TestClient(m.app) as client:
        client.post("/api/resources", json={
            "resource_id": "off-res", "name": "Off", "provider": "deepseek"})
        client.put("/api/resources/off-res", json={
            "name": "Off", "provider": "deepseek", "enabled": False})
        r = client.post("/gateway/deepseek/chat/completions",
                        json={"model": "deepseek-chat", "messages": []},
                        headers={"X-Monitor-Resource": "off-res"})
        assert r.status_code == 400
        assert "disabled" in r.text
        # 未产生事件
        assert m.store.recent_events(5) == []


def test_unknown_resource_still_rejected(gw):
    m = gw
    with TestClient(m.app) as client:
        r = client.post("/gateway/deepseek/chat/completions",
                        json={"model": "deepseek-chat", "messages": []},
                        headers={"X-Monitor-Resource": "never-existed"})
        assert r.status_code == 400
        assert "unknown resource" in r.text


def test_no_header_request_still_works(gw):
    """无 header 兼容：请求成功、resource_id NULL、pricing 正常。"""
    m = gw
    with TestClient(m.app) as client:
        r = client.post("/gateway/deepseek/chat/completions",
                        json={"model": "deepseek-chat", "messages": []})
        assert r.status_code == 200
        e = m.store.recent_events(1)[0]
        assert e["resource_id"] is None
        assert e["estimated_cost"] is not None


# ---------- Dashboard 数据（无需重启） ----------

def test_new_resource_appears_in_usage_without_restart(app):
    c, m = app
    assert c.get("/api/resources/usage").json()["resources"] == []
    c.post("/api/resources", json={"resource_id": "vis-1", "name": "Vis1",
                                   "provider": "deepseek"})
    d = c.get("/api/resources/usage").json()
    vis = [r for r in d["resources"] if r["resource_id"] == "vis-1"]
    assert len(vis) == 1 and vis[0]["requests"] == 0 and vis[0]["cost_status"] == "none"


def test_edited_resource_updates_in_usage(app):
    c, m = app
    c.post("/api/resources", json={"resource_id": "e1", "name": "E1",
                                   "provider": "deepseek",
                                   "resource_type": "api",
                                   "billing_mode": "prepaid"})
    c.put("/api/resources/e1", json={"name": "E1 Renamed",
                                     "provider": "deepseek",
                                     "resource_type": "quota",
                                     "billing_mode": "free"})
    d = c.get("/api/resources/usage").json()
    e1 = [r for r in d["resources"] if r["resource_id"] == "e1"][0]
    assert e1["resource_name"] == "E1 Renamed"
    assert e1["resource_type"] == "quota" and e1["billing_mode"] == "free"


def test_disabled_resource_remains_visible(app):
    c, m = app
    c.post("/api/resources", json={"resource_id": "d1", "name": "D1",
                                   "provider": "deepseek"})
    c.put("/api/resources/d1", json={"name": "D1", "provider": "deepseek",
                                     "enabled": False})
    d = c.get("/api/resources/usage").json()
    assert [r for r in d["resources"] if r["resource_id"] == "d1"]  # 仍可见
