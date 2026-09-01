"""P5-C：Attribution 可解释性 & 多 Resource 归因覆盖 — regression 测试。

覆盖：
- 可解释归因层级（绝不猜 Provider→Resource）：
  explicit_header > provider_default(用户声明) > unique_resource > unattributed
- 每一笔事件保留 attribution_source（落 metadata，复用既有字段，不改 schema）
- 多 Resource 同 Provider 时，provider default_resource_id 提升覆盖率（不猜）
- config.example.yaml 必须给出 resources 模板（含多 Resource 同 Provider 示例）
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
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402
import monitor.gateway as gw  # noqa: E402
import monitor.main as m  # noqa: E402
from monitor.config import ConfigManager  # noqa: E402
from monitor.core import MonitorCore  # noqa: E402
from monitor.events import AIRequestEvent  # noqa: E402
from monitor.resource import ResourceDefinition, ResourceRegistry  # noqa: E402
from monitor.storage import EventStore  # noqa: E402


# --------------------------------------------------------------------------
# 配置环境 fixture：ConfigManager + ResourceRegistry（共享 resources dict）
# --------------------------------------------------------------------------

@pytest.fixture()
def cfg_env(tmp_path):
    cm = ConfigManager(tmp_path / "config.yaml")
    res = ResourceRegistry(cm.resources)
    return cm, res


def _def(resource_id, provider="deepseek", billing_mode="prepaid"):
    return ResourceDefinition(
        resource_id=resource_id, name=resource_id, provider=provider,
        resource_type="api", billing_mode=billing_mode, enabled=True)


# --------------------------------------------------------------------------
# 纯函数：_resolve_resource_id 归因层级
# --------------------------------------------------------------------------

def test_resolve_explicit_header(cfg_env):
    cm, res = cfg_env
    cm.upsert_resource(_def("deepseek-paid"))
    rid, reject, src = gw._resolve_resource_id(
        "deepseek", "deepseek-paid", res, cm)
    assert rid == "deepseek-paid" and reject is None and src == "explicit_header"


def test_resolve_explicit_unknown_resource_rejected(cfg_env):
    cm, res = cfg_env
    rid, reject, src = gw._resolve_resource_id("deepseek", "ghost", res, cm)
    assert rid is None and reject is not None and reject.status_code == 400
    assert src == "rejected_unknown"


def test_resolve_explicit_disabled_resource_rejected(cfg_env):
    cm, res = cfg_env
    cm.upsert_resource(ResourceDefinition(
        resource_id="deepseek-off", name="x", provider="deepseek",
        resource_type="api", billing_mode="free", enabled=False))
    rid, reject, src = gw._resolve_resource_id("deepseek", "deepseek-off", res, cm)
    assert rid is None and reject is not None and reject.status_code == 400
    assert src == "rejected_disabled"


def test_resolve_provider_default_multi_resource(cfg_env):
    """多 Resource 同 Provider 时，用户声明的 default_resource_id 提升覆盖率（不猜）。"""
    cm, res = cfg_env
    cm.upsert_resource(_def("deepseek-paid"))
    cm.upsert_resource(_def("deepseek-free", billing_mode="free"))
    cm.upsert("deepseek", enabled=True, default_resource_id="deepseek-paid")
    rid, reject, src = gw._resolve_resource_id("deepseek", None, res, cm)
    assert rid == "deepseek-paid" and reject is None and src == "provider_default"


def test_resolve_unique_enabled_resource(cfg_env):
    """该 Provider 恰好一个 enabled Resource → 确定性归因。"""
    cm, res = cfg_env
    cm.upsert_resource(_def("deepseek-paid"))
    rid, reject, src = gw._resolve_resource_id("deepseek", None, res, cm)
    assert rid == "deepseek-paid" and reject is None and src == "unique_resource"


def test_resolve_unattributed_when_ambiguous(cfg_env):
    """多 enabled Resource 且未声明 default → 未归因（绝不猜）。"""
    cm, res = cfg_env
    cm.upsert_resource(_def("deepseek-paid"))
    cm.upsert_resource(_def("deepseek-free", billing_mode="free"))
    rid, reject, src = gw._resolve_resource_id("deepseek", None, res, cm)
    assert rid is None and reject is None and src == "unattributed"


def test_resolve_provider_default_ignores_unknown_or_disabled(cfg_env):
    """default_resource_id 指向不存在/已禁用的 Resource 时，降级到下层（不崩溃、不误归）。"""
    cm, res = cfg_env
    cm.upsert_resource(_def("deepseek-paid"))
    cm.upsert_resource(_def("deepseek-free", billing_mode="free"))
    cm.upsert("deepseek", enabled=True, default_resource_id="ghost")
    rid, reject, src = gw._resolve_resource_id("deepseek", None, res, cm)
    # ghost 不存在 → 跳过 provider_default → 唯一 enabled? 否（2 个）→ unattributed
    assert rid is None and reject is None and src == "unattributed"


# --------------------------------------------------------------------------
# 可解释性：attribution_source 进入事件 metadata 并经由 normalize 往返
# --------------------------------------------------------------------------

def test_build_event_records_attribution_source():
    class FakeReq:
        headers = {}

    class FakeAdapter:
        def extract_model(self, *a):
            return "deepseek-chat"

    ev = gw._build_event(
        "deepseek", "chat/completions", FakeReq(), {}, FakeAdapter(),
        resource_id="deepseek-paid", attribution_source="provider_default")
    assert ev.resource_id == "deepseek-paid"
    # P6-E 增强：metadata 现同时记录 project/client 归因来源（无 header/UA 时
    # 为 unattributed）。这是有意的可解释性增强，不再仅有单一 attribution_source。
    assert ev.metadata["attribution_source"] == "provider_default"
    assert ev.metadata["project_attribution_source"] == "unattributed"
    assert ev.metadata["client_attribution_source"] == "unattributed"


def test_attribution_source_survives_normalize():
    """gateway 写入链路：event.to_dict() → core.normalize 必须保留 metadata。"""
    ev = AIRequestEvent(
        provider="deepseek", resource_id="deepseek-paid",
        metadata={"attribution_source": "provider_default"})
    core = MonitorCore(store=None)  # normalize 不依赖 store
    norm = core.normalize(ev.to_dict())
    assert norm.resource_id == "deepseek-paid"
    assert norm.metadata == {"attribution_source": "provider_default"}


# --------------------------------------------------------------------------
# 端到端：真实网关链路记录 provider_default 归因 + attribution_source
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


def test_provider_default_attribution_end_to_end(gateway_url):
    client, store = gateway_url
    # 注册 resource（直接 upsert 到 gateway 可见的 registry）
    rd = _def("deepseek-paid")
    m.resources.upsert(rd)
    m.config_mgr.upsert_resource(rd)
    # 声明 provider default
    m.config_mgr.upsert("deepseek", default_resource_id="deepseek-paid")
    r = client.post("/gateway/deepseek/chat/completions",
                    json={"model": "deepseek-chat", "messages": []})
    assert r.status_code == 200
    evs = store.recent_events(limit=20)
    ev = next(e for e in evs if e["provider"] == "deepseek")
    assert ev["resource_id"] == "deepseek-paid"
    meta = ev["metadata"]
    assert isinstance(meta, dict)
    assert meta.get("attribution_source") == "provider_default"


# --------------------------------------------------------------------------
# 配置模板：config.example.yaml 必须包含 resources 段（多 Resource 同 Provider）
# --------------------------------------------------------------------------

def test_config_example_resources_section():
    p = Path(__file__).resolve().parent.parent / "config.example.yaml"
    data = yaml.safe_load(p.read_text(encoding="utf-8"))
    assert "resources" in data
    res = data["resources"]
    assert res["deepseek-paid"]["provider"] == "deepseek"
    # 一个 Provider 多个 Resource 被明确支持
    assert res["deepseek-free"]["provider"] == "deepseek"
    assert res["openai-main"]["provider"] == "openai"
    # providers 段允许声明 default_resource_id（可解释归因映射）
    assert "providers" in data and "deepseek" in data["providers"]
    # 归因机制在注释中说明
    assert "X-Monitor-Resource" in p.read_text(encoding="utf-8")
