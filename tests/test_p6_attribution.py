"""P6 — Usage & Attribution Completeness：regression 测试。

覆盖链路：
- P6-D Project 归因：显式 X-Monitor-Project > configured default_project > 未归因（NULL）
- P6-E Client 归因：显式 X-Monitor-Client > 已知 User-Agent 推导 > 未归因（NULL，绝不猜）
- P6-B Cache 提取：OpenAI details.cached_tokens / DeepSeek prompt_cache_hit_tokens /
  Gemini usageMetadata.cachedContentTokenCount / 缺失 → None
- P6-F 自定义 OpenAI-compatible Provider 运行时路由（复用 OpenAICompatibleAdapter，不另起代理）
- P6-C 成本按每 1M token 计（pricing 除以 1_000_000）
- 安全：凭据绝不落 config / 前端；归因不泄露 secret；NULL 绝不变 0

所有测试通过 conftest 的 MONITOR_DATA_DIR 隔离，绝不触碰生产 data/monitor.db。
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

import monitor.gateway as gw  # noqa: E402
import monitor.main as m  # noqa: E402
from monitor.config import ConfigManager  # noqa: E402
from monitor.core import MonitorCore  # noqa: E402
from monitor.events import AIRequestEvent, Usage  # noqa: E402
from monitor.pricing import PricingRegistry  # noqa: E402
from monitor.providers import OpenAICompatibleAdapter, GenericAdapter  # noqa: E402
from monitor.storage import EventStore  # noqa: E402


# ==========================================================================
# P6-D Project 归因（绝不猜）
# ==========================================================================

def test_resolve_project_explicit_header():
    res = gw._resolve_project_attribution(
        "deepseek", {"x-monitor-project": "Research"}, None)
    assert res == ("Research", "explicit_header")


def test_resolve_project_configured_default():
    class Cfg:
        default_project = "PAL"
    res = gw._resolve_project_attribution("deepseek", {}, Cfg())
    assert res == ("PAL", "configured_default")


def test_resolve_project_unattributed_is_null():
    res = gw._resolve_project_attribution("deepseek", {}, None)
    assert res == (None, "unattributed")


def test_build_event_records_project_attribution():
    class FakeReq:
        headers = {"x-monitor-project": "Atlas"}

    class FakeAdapter:
        def extract_model(self, *a):
            return "deepseek-chat"

    ev = gw._build_event(
        "deepseek", "chat/completions", FakeReq(), {}, FakeAdapter())
    assert ev.project == "Atlas"
    assert ev.metadata["project_attribution_source"] == "explicit_header"
    # 同时验证 client 未归因时保持 NULL（不猜）
    assert ev.client is None
    assert ev.metadata["client_attribution_source"] == "unattributed"


# ==========================================================================
# P6-E Client 归因（绝不猜；仅显式头 / 已知 UA，绝不用 provider/模型/key 推导）
# ==========================================================================

def test_classify_user_agent_known():
    assert gw._classify_user_agent("Mozilla/5.0 codex/1.0") == "codex"
    assert gw._classify_user_agent("WorkBuddy/2.0") == "workbuddy"
    assert gw._classify_user_agent("openai-python/1.3") == "openai-sdk"
    assert gw._classify_user_agent("curl/8.0") == "curl"


def test_classify_user_agent_unknown_returns_none():
    assert gw._classify_user_agent("mystery-client/9.9") is None
    assert gw._classify_user_agent("") is None


def test_resolve_client_explicit_header():
    res = gw._resolve_client_attribution({"x-monitor-client": "codex"})
    assert res == ("codex", "explicit_header")


def test_resolve_client_from_user_agent():
    res = gw._resolve_client_attribution({"user-agent": "openai-python/1.0"})
    assert res == ("openai-sdk", "user_agent")


def test_resolve_client_unknown_ua_is_null():
    res = gw._resolve_client_attribution({"user-agent": "totally-unknown/1.0"})
    assert res == (None, "unattributed")


def test_build_event_records_client_attribution():
    class FakeReq:
        headers = {"user-agent": "openai-python/1.0"}

    class FakeAdapter:
        def extract_model(self, *a):
            return "deepseek-chat"

    ev = gw._build_event(
        "deepseek", "chat/completions", FakeReq(), {}, FakeAdapter())
    assert ev.client == "openai-sdk"
    assert ev.metadata["client_attribution_source"] == "user_agent"
    assert ev.metadata["attribution_source"] == "unattributed"


# ==========================================================================
# P6-B Cache 提取（绝不估算；上游未返回则 None）
# ==========================================================================

def test_openai_compat_cache_details_cached_tokens():
    resp = {"usage": {"prompt_tokens": 100,
                      "prompt_tokens_details": {"cached_tokens": 80},
                      "completion_tokens": 10, "total_tokens": 110}}
    assert OpenAICompatibleAdapter("x", "http://x").extract_cache_usage(resp) == \
        {"cache_read_tokens": 80, "cache_write_tokens": None}


def test_openai_compat_cache_deepseek_prompt_cache_hit():
    resp = {"usage": {"prompt_tokens": 100, "prompt_cache_hit_tokens": 50,
                      "prompt_cache_write_tokens": 5,
                      "completion_tokens": 10, "total_tokens": 110}}
    assert OpenAICompatibleAdapter("x", "http://x").extract_cache_usage(resp) == \
        {"cache_read_tokens": 50, "cache_write_tokens": 5}


def test_openai_compat_cache_missing_is_none():
    resp = {"usage": {"prompt_tokens": 100, "completion_tokens": 10,
                      "total_tokens": 110}}
    assert OpenAICompatibleAdapter("x", "http://x").extract_cache_usage(resp) is None


def test_generic_cache_openai_style():
    resp = {"usage": {"prompt_tokens": 100,
                      "prompt_tokens_details": {"cached_tokens": 70},
                      "completion_tokens": 10, "total_tokens": 110}}
    assert GenericAdapter().extract_cache_usage(resp) == \
        {"cache_read_tokens": 70, "cache_write_tokens": None}


def test_generic_cache_gemini_style():
    resp = {"usageMetadata": {"promptTokenCount": 100,
                              "cachedContentTokenCount": 60,
                              "candidatesTokenCount": 10,
                              "totalTokenCount": 110}}
    assert GenericAdapter().extract_cache_usage(resp) == \
        {"cache_read_tokens": 60, "cache_write_tokens": None}


def test_generic_cache_missing_is_none():
    assert GenericAdapter().extract_cache_usage({"usage": {}}) is None
    assert GenericAdapter().extract_cache_usage({"other": 1}) is None


# ==========================================================================
# P6-C 成本按每 1M token 计
# ==========================================================================

def test_cost_is_per_million_tokens(tmp_path):
    price_yaml = tmp_path / "p.yaml"
    price_yaml.write_text(yaml.safe_dump({
        "mytest": {"currency": "USD", "models": {
            "test-model": {"input": 10.0, "output": 20.0}}}},
        allow_unicode=True), encoding="utf-8")
    reg = PricingRegistry(price_yaml)
    # 1M input, 0 output -> 10.0
    c1 = reg.compute_cost("mytest", "test-model",
                          Usage(input_tokens=1_000_000, output_tokens=0))
    assert c1.amount == 10.0 and c1.currency == "USD"
    # 500k input, 200k output -> 0.5*10 + 0.2*20 = 9.0
    c2 = reg.compute_cost("mytest", "test-model",
                          Usage(input_tokens=500_000, output_tokens=200_000))
    assert c2.amount == 9.0
    # 无价格 -> None（不估算）
    assert reg.compute_cost("unknown", "x", Usage(input_tokens=1)) is None


# ==========================================================================
# P6-F 自定义 OpenAI-compatible Provider 运行时路由（e2e）
# ==========================================================================

def _make_upstream(usage=None, cache=None):
    usage = usage or {"prompt_tokens": 12, "completion_tokens": 6,
                     "total_tokens": 18}
    if cache:
        usage = {**usage, **cache}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            length = int(self.headers.get("content-length") or 0)
            body = json.loads(self.rfile.read(length) or b"{}")
            data = json.dumps({
                "id": "chatcmpl-1", "model": body.get("model"),
                "choices": [{"message": {"role": "assistant",
                                         "content": "ok"}}],
                "usage": usage}).encode()
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


@pytest.fixture()
def gateway_url(tmp_path, monkeypatch):
    upstream = _make_upstream()
    monkeypatch.setattr(m, "config_mgr", ConfigManager(tmp_path / "config.yaml"))
    monkeypatch.setattr(m, "store", EventStore(tmp_path / "test.db"))
    monkeypatch.setenv("MONITOR_DATA_DIR", str(tmp_path))
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
    yield httpx.Client(base_url=url, timeout=30), upstream, m.store
    server.should_exit = True
    t.join(timeout=5)
    upstream.shutdown()


def test_custom_openai_compatible_provider_routed(gateway_url, monkeypatch):
    """未在 registry 注册、但 config 配置了 base_url + enabled 的 Provider，
    应复用 OpenAICompatibleAdapter 路由（不另起第二套代理），并记录 provider。"""
    client, upstream, store = gateway_url
    # 通过 HTTP 接口配置自定义 provider（PUT /api/providers/{name}）
    cfg_resp = client.put(
        "/api/providers/private-llm",
        json={"enabled": True,
              "base_url": f"http://127.0.0.1:{upstream.server_port}"})
    assert cfg_resp.status_code == 200
    assert cfg_resp.json()["base_url"].endswith(str(upstream.server_port))
    # 提供凭据（环境变量；凭据边界不变）
    monkeypatch.setenv("PRIVATE_LLM_API_KEY", "sk-test")
    r = client.post("/gateway/private-llm/v1/chat/completions",
                    json={"model": "local-model", "messages": []})
    assert r.status_code == 200, r.text
    evs = store.recent_events(limit=20)
    ev = next(e for e in evs if e["provider"] == "private-llm")
    assert ev["model"] == "local-model"
    # 默认无 X-Monitor-Resource / 多 Resource 判定 -> 未归因（NULL，不猜）
    assert ev["resource_id"] is None or ev["resource_id"] == ""


def test_client_attribution_end_to_end(gateway_url, monkeypatch):
    client, upstream, store = gateway_url
    m.config_mgr.upsert("deepseek", enabled=True,
                        base_url=f"http://127.0.0.1:{upstream.server_port}",
                        api_key="sk-test")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
    # 已知 User-Agent -> openai-sdk
    r = client.post("/gateway/deepseek/chat/completions",
                    json={"model": "deepseek-chat", "messages": []},
                    headers={"user-agent": "openai-python/1.0"})
    assert r.status_code == 200
    # 显式 X-Monitor-Client -> 优先
    r2 = client.post("/gateway/deepseek/chat/completions",
                     json={"model": "deepseek-chat", "messages": []},
                     headers={"x-monitor-client": "codex"})
    assert r2.status_code == 200
    evs = store.recent_events(limit=20)
    by_client = {e["client"]: e for e in evs if e["provider"] == "deepseek"}
    assert by_client.get("openai-sdk") is not None
    assert by_client.get("codex") is not None
    meta = by_client["codex"]["metadata"]
    assert meta["client_attribution_source"] == "explicit_header"


def test_project_default_attribution_end_to_end(gateway_url, monkeypatch):
    client, upstream, store = gateway_url
    m.config_mgr.upsert("deepseek", enabled=True,
                        base_url=f"http://127.0.0.1:{upstream.server_port}",
                        api_key="sk-test", default_project="PAL")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
    r = client.post("/gateway/deepseek/chat/completions",
                    json={"model": "deepseek-chat", "messages": []})
    assert r.status_code == 200
    ev = next(e for e in store.recent_events(limit=20)
              if e["provider"] == "deepseek")
    assert ev["project"] == "PAL"
    assert ev["metadata"]["project_attribution_source"] == "configured_default"


# ==========================================================================
# 安全：凭据绝不落 config / 前端；归因不泄露 secret；NULL 绝不变 0
# ==========================================================================

def test_config_save_strips_api_key(tmp_path):
    p = tmp_path / "config.yaml"
    p.write_text(yaml.safe_dump({
        "providers": {"deepseek": {"enabled": True, "api_key": "sk-SECRET-XYZ",
                                   "base_url": "https://api.deepseek.com"}}},
        allow_unicode=True), encoding="utf-8")
    cm = ConfigManager(p)
    cm.save()  # load 已丢弃明文 key；save 亦不写回
    reloaded = yaml.safe_load(p.read_text(encoding="utf-8"))
    assert "api_key" not in reloaded["providers"]["deepseek"]
    assert cm.get("deepseek").api_keys == []


def test_upsert_ignores_api_key(tmp_path):
    cm = ConfigManager(tmp_path / "config.yaml")
    cm.upsert("deepseek", enabled=True, api_key="sk-SECRET",
              base_url="https://api.deepseek.com")
    assert cm.get("deepseek").api_keys == []
    raw = yaml.safe_load((tmp_path / "config.yaml").read_text(encoding="utf-8"))
    assert "api_key" not in raw["providers"]["deepseek"]


def test_public_view_has_no_key(tmp_path):
    cm = ConfigManager(tmp_path / "config.yaml")
    cm.upsert("deepseek", enabled=True, base_url="https://api.deepseek.com")
    for v in cm.public_view():
        assert "api_key" not in v
        assert "api_keys" not in v


def test_event_snapshot_excludes_secrets():
    ev = AIRequestEvent(provider="deepseek", model="deepseek-chat",
                        input_tokens=10, output_tokens=5)
    snap = gw._event_snapshot(ev)
    # 绝不含任何 prompt / response / key 字段
    for forbidden in ("api_key", "prompt", "messages", "content", "authorization"):
        assert forbidden not in snap


def test_distinct_dim_values_client(tmp_path):
    store = EventStore(tmp_path / "t.db")
    for c in ("codex", "openai-sdk", None, "workbuddy"):
        store.insert(AIRequestEvent(provider="deepseek", model="m",
                                    client=c, input_tokens=1, output_tokens=1))
    clients = store.distinct_dim_values("client")
    # COALESCE(client,'Unknown') -> None 显示为 'Unknown'，真实 client 都在
    assert "codex" in clients and "openai-sdk" in clients and "workbuddy" in clients
    # 非法维度必须报错（不静默）
    with pytest.raises(ValueError):
        store.distinct_dim_values("not_a_dim")


def test_unknown_attribution_null_not_zero(tmp_path):
    """未归因的 client/project 落库为 NULL，绝不存成 '0' / 'None' 字符串。"""
    store = EventStore(tmp_path / "t.db")
    store.insert(AIRequestEvent(provider="deepseek", model="m",
                                client=None, project=None,
                                input_tokens=1, output_tokens=1))
    ev = store.recent_events(limit=1)[0]
    assert ev["client"] is None
    assert ev["project"] is None
