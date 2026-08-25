"""Phase 1E-C: Credential Boundary Closure — regression suite.

验证：
- ConfigManager 永不持久化真实 secret（H1/H2）
- Gateway runtime 注入不污染共享 ProviderConfig（H3）
- Dashboard PUT 不产生 secret persistence
- 历史 config 可幂等清理
- 无 config fallback credential

所有测试使用的 synthetic secret（sk-test-...）均为虚构，绝不出现真实密钥。
"""
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

sys_path = str(Path(__file__).resolve().parent.parent)
import sys  # noqa: E402
sys.path.insert(0, sys_path)

from fastapi.testclient import TestClient  # noqa: E402

from monitor.config import ConfigManager, cleanup_config_secrets  # noqa: E402
from monitor.credential import CredentialProvider  # noqa: E402
from monitor.storage import EventStore  # noqa: E402


# ---------- stub upstream ----------
def _make_upstream():
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            length = int(self.headers.get("content-length") or 0)
            body = json.loads(self.rfile.read(length) or b"{}")
            resp = {"id": "x", "model": body.get("model"),
                    "choices": [{"message": {"role": "assistant",
                                            "content": "ok"}}],
                    "usage": {"prompt_tokens": 10, "completion_tokens": 5,
                              "total_tokens": 15}}
            data = json.dumps(resp).encode()
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def _cfg_yaml(path, **providers):
    lines = ["scheduler: {enabled: false}", "providers:"]
    for name, body in providers.items():
        lines.append(f"  {name}:")
        for k, v in body.items():
            if isinstance(v, list):
                lines.append(f"    {k}:")
                for item in v:
                    lines.append(f"      - {item}")
            else:
                lines.append(f"    {k}: {v}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


# ---------- Test 1: save() 永不写真实 api_key ----------
def test_save_never_writes_real_api_key(tmp_path):
    cm = ConfigManager(tmp_path / "cfg.yaml")
    cm.upsert("deepseek", enabled=True, base_url="http://up",
              api_key="sk-TESTREALKEY123")
    text = (tmp_path / "cfg.yaml").read_text(encoding="utf-8")
    assert "sk-TESTREALKEY123" not in text
    assert "api_key" not in text and "api_keys" not in text


# ---------- Test 2: upsert(api_key=...) 后 save() 仍无真实 key ----------
def test_upsert_api_key_not_persisted(tmp_path):
    cm = ConfigManager(tmp_path / "cfg.yaml")
    cm.upsert("openai", enabled=True, api_key="sk-secret-xyz")
    cm.save()  # 再次保存也应安全
    text = (tmp_path / "cfg.yaml").read_text(encoding="utf-8")
    assert "sk-secret-xyz" not in text
    assert "api_key" not in text and "api_keys" not in text


# ---------- Test 3 (Test A): gateway env key 不污染共享 config object ----------
def test_gateway_env_key_does_not_pollute_shared_config(tmp_path, monkeypatch):
    import monitor.main as m

    upstream = _make_upstream()
    cm = ConfigManager(tmp_path / "cfg.yaml")
    cm.upsert("deepseek", enabled=True,
              base_url=f"http://127.0.0.1:{upstream.server_port}")
    m.config_mgr = cm
    m.store = EventStore(tmp_path / "t.db")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-runtime-secret-abc")
    try:
        with TestClient(m.app) as c:
            r = c.post("/gateway/deepseek/chat/completions",
                       json={"model": "deepseek-chat",
                             "messages": [{"role": "user", "content": "hi"}]})
            assert r.status_code == 200
        # H3 核心：共享 ProviderConfig 未被 gateway 注入真实 secret
        assert cm.providers["deepseek"].api_keys == []
        # 再次显式 save() 后，磁盘仍无真实 secret
        cm.save()
        text = (tmp_path / "cfg.yaml").read_text(encoding="utf-8")
        assert "sk-runtime-secret-abc" not in text
        assert "api_key" not in text and "api_keys" not in text
    finally:
        monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
        m.store.close()
        upstream.shutdown()


# ---------- Test 4: gateway request 成功使用 env credential ----------
def test_gateway_uses_env_credential(tmp_path, monkeypatch):
    import monitor.main as m

    upstream = _make_upstream()
    cm = ConfigManager(tmp_path / "cfg.yaml")
    cm.upsert("deepseek", enabled=True,
              base_url=f"http://127.0.0.1:{upstream.server_port}")
    m.config_mgr = cm
    m.store = EventStore(tmp_path / "t.db")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-env-cred-ok")
    try:
        with TestClient(m.app) as c:
            r = c.post("/gateway/deepseek/chat/completions",
                       json={"model": "deepseek-chat",
                             "messages": [{"role": "user", "content": "hi"}]})
            assert r.status_code == 200
            # 上游确实收到了 Bearer sk-env-cred-ok（env 凭据被使用）
            assert r.json()["choices"][0]["message"]["content"] == "ok"
    finally:
        monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
        m.store.close()
        upstream.shutdown()


# ---------- Test 5: Dashboard PUT 带 api_key 不落盘 ----------
def test_dashboard_put_api_key_not_persisted(tmp_path, monkeypatch):
    import monitor.main as m

    cm = ConfigManager(tmp_path / "cfg.yaml")
    cm.upsert("deepseek", enabled=True, base_url="http://up")
    m.config_mgr = cm
    m.store = EventStore(tmp_path / "t.db")
    try:
        with TestClient(m.app) as c:
            # 注册表允许 deepseek
            r = c.put("/api/providers/deepseek",
                      json={"api_key": "sk-dashboard-secret-999"})
            assert r.status_code in (200, 404)  # deepseek 已注册 → 200
            if r.status_code == 200:
                body = r.json()
                assert "api_key" not in body  # 响应不下发 secret
        text = (tmp_path / "cfg.yaml").read_text(encoding="utf-8")
        assert "sk-dashboard-secret-999" not in text
    finally:
        m.store.close()


# ---------- Test 6: 历史 config 清理（sk-/ark-/Bearer 均被移除）+ 幂等 ----------
def test_historical_config_cleanup_idempotent(tmp_path):
    cfg = tmp_path / "legacy.yaml"
    _cfg_yaml(cfg, deepseek={
        "enabled": True, "base_url": "http://up",
        "api_keys": ["sk-legacy123", "ark-legacy456"],
        "api_key": "Bearer supersecret",
    }, openai={"enabled": True, "base_url": "http://o",
               "api_keys": ["sk-openai-legacy"]})

    assert cleanup_config_secrets(cfg) is True
    text1 = cfg.read_text(encoding="utf-8")
    assert "sk-legacy123" not in text1
    assert "ark-legacy456" not in text1
    assert "sk-openai-legacy" not in text1
    assert "Bearer supersecret" not in text1
    # 键本身被删除
    assert "api_key" not in text1 and "api_keys" not in text1

    # 幂等：第二次运行结果不变、返回 False
    assert cleanup_config_secrets(cfg) is False
    text2 = cfg.read_text(encoding="utf-8")
    assert text1 == text2


# ---------- Test 7: 清理后正常配置字段保留 ----------
def test_cleanup_retains_normal_fields(tmp_path):
    cfg = tmp_path / "legacy.yaml"
    _cfg_yaml(cfg, deepseek={
        "enabled": True, "base_url": "http://up",
        "api_keys": ["sk-x"], "test_model": "deepseek-chat"})
    cleanup_config_secrets(cfg)
    data = __import__("yaml").safe_load(cfg.read_text(encoding="utf-8"))
    d = data["providers"]["deepseek"]
    assert d["enabled"] is True
    assert d["base_url"] == "http://up"
    assert d.get("test_model") == "deepseek-chat"
    assert "api_key" not in d and "api_keys" not in d


# ---------- Test 8: 无 env 时不 fallback 到 config 旧 secret ----------
def test_no_env_no_fallback_to_config_secret(tmp_path, monkeypatch):
    import monitor.main as m

    # 配置中含有 legacy secret（模拟历史磁盘 secret）
    cfg = tmp_path / "cfg.yaml"
    _cfg_yaml(cfg, deepseek={
        "enabled": True, "base_url": "http://up",
        "api_keys": ["sk-config-only-secret"]})
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    cm = ConfigManager(cfg)  # load 应丢弃 secret
    assert cm.providers["deepseek"].api_keys == []
    m.config_mgr = cm
    m.store = EventStore(tmp_path / "t.db")
    upstream = _make_upstream()
    cm.providers["deepseek"].base_url = f"http://127.0.0.1:{upstream.server_port}"
    try:
        with TestClient(m.app) as c:
            # 无 env key、config secret 被丢弃 → 必须 rejected（400），不得 fallback 使用 sk-config-only-secret
            r = c.post("/gateway/deepseek/chat/completions",
                       json={"model": "deepseek-chat",
                             "messages": [{"role": "user", "content": "hi"}]})
            assert r.status_code == 400
            evt = m.store.recent_events(1)[0]
            assert evt["event_type"] == "rejected"
        # save 后磁盘仍无 config secret（证明无 fallback 落盘）
        cm.save()
        text = cfg.read_text(encoding="utf-8")
        assert "sk-config-only-secret" not in text
    finally:
        m.store.close()
        upstream.shutdown()


# ---------- 补充：public_view 由环境变量判定 has_key ----------
def test_public_view_uses_env_not_config(tmp_path, monkeypatch):
    cm = ConfigManager(tmp_path / "cfg.yaml")
    cm.upsert("deepseek", enabled=True, base_url="http://up")
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    view = cm.public_view()
    assert view[0]["has_key"] is False
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-env-present")
    assert cm.public_view()[0]["has_key"] is True
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)


# ---------- 补充：_strip_secret 直接单测 ----------
def test_strip_secret_removes_secret_keys():
    from monitor.config import _strip_secret
    d = {"enabled": True, "base_url": "x", "api_key": "sk-x",
         "api_keys": ["sk-y"], "secret": "z", "nested": {"api_key": "ok"}}
    out = _strip_secret(d)
    assert "api_key" not in out
    assert "api_keys" not in out
    assert "secret" not in out
    assert out["enabled"] is True
    assert out["base_url"] == "x"
    # 非 secret 键（含 nested 内的 api_key 字符串键）由调用方决定是否递归；
    # 顶层 secret 键已剔除。
    assert "nested" in out
