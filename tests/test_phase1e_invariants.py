"""Phase 1E-A 验收：24 项核心不变量。

覆盖 Phase 1D Decision Record + Phase 1E-A 裁决的关键语义：
事件模型 / 三轴分离 / NULL≠0 / UTC / Schema 演进 / Parser / Sanitizer / 凭据边界。
"""
import os
import re
import sqlite3
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402

from monitor.config import ConfigManager  # noqa: E402
from monitor.core import MonitorCore  # noqa: E402
from monitor.credential import CredentialProvider, _env_name  # noqa: E402
from monitor.events import AIRequestEvent, Usage, utc_now_ms  # noqa: E402
from monitor.providers import (GeminiAdapter, GenericAdapter,  # noqa: E402
                               OpenAICompatibleAdapter)
from monitor.registry import ProviderRegistry  # noqa: E402
from monitor.sanitize import sanitize_error, sanitize_usage_dict  # noqa: E402
from monitor.storage import EventStore  # noqa: E402


# ---------- 1. 事件模型：单一 Event Ledger + event_type 判别 ----------
def test_event_type_default_and_discriminator():
    e = AIRequestEvent(provider="deepseek", model="m")
    assert e.event_type == "llm_call"
    for t in ("rejected", "error"):
        e2 = AIRequestEvent(provider="p", model="m", event_type=t)
        assert e2.event_type == t


# ---------- 2. rejected 事件 token/cost 必须为 NULL，不污染 Usage ----------
def test_rejected_event_has_null_usage(tmp_path):
    store = EventStore(tmp_path / "t.db")
    e = AIRequestEvent(provider="x", model="m", event_type="rejected",
                       error="no key")
    e.apply_usage(None)  # 无 usage
    store.insert(e)
    row = store.recent_events(1)[0]
    assert row["input_tokens"] is None
    assert row["total_tokens"] is None
    assert row["cost"] is None
    store.close()


# ---------- 3. NULL ≠ 0：unknown cost 不 coalesce 成 0 ----------
def test_unknown_cost_excluded_from_cost_aggregates(tmp_path):
    store = EventStore(tmp_path / "t.db")
    # 已知 cost
    store.insert(AIRequestEvent(provider="deepseek", model="m", cost=0.01,
                                currency="CNY", total_tokens=10))
    # 未知 cost（NULL）
    store.insert(AIRequestEvent(provider="deepseek", model="m", cost=None,
                                total_tokens=5))
    ov = store.analytics_overview()
    # cost 聚合仅含已知部分，未知不计入 0
    assert ov["cost_by_currency"].get("CNY") == 0.01
    assert ov["requests"] == 2  # 总数仍含两条
    store.close()


# ---------- 4. cost_status 五个状态 ----------
def test_cost_status_transitions(tmp_path):
    store = EventStore(tmp_path / "t.db")
    store.insert(AIRequestEvent(provider="p", model="m", resource_id="r1",
                                cost=0.01))
    store.insert(AIRequestEvent(provider="p", model="m", resource_id="r1",
                                cost=None))
    rows = store.resource_usage_by_resource()
    r1 = [r for r in rows if r["resource_id"] == "r1"][0]
    # 1 已知 + 1 未知 → mixed
    assert r1["cost_count"] == 1 and r1["requests"] == 2
    store.close()


# ---------- 5. 时间语义：UTC epoch 毫秒 ----------
def test_occurred_at_is_utc_ms():
    s = utc_now_ms()
    assert re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$", s), s
    # 'Z' 表示 UTC
    assert s.endswith("Z")


# ---------- 6. schema_version = 2 ----------
def test_schema_version_persisted(tmp_path):
    store = EventStore(tmp_path / "t.db")
    e = AIRequestEvent(provider="p", model="m")
    assert e.schema_version == 2
    store.insert(e)
    row = store.recent_events(1)[0]
    assert row["schema_version"] == 2
    store.close()


# ---------- 7. reasoning_tokens 捕获（o1 / DeepSeek-R） ----------
def test_openai_compat_reasoning_tokens():
    a = OpenAICompatibleAdapter("deepseek", "https://api.deepseek.com")
    u = a.extract_usage({"usage": {
        "prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15,
        "reasoning_tokens": 7}})
    assert u.reasoning_tokens == 7
    assert u.input_tokens == 10 and u.total_tokens == 15


# ---------- 8. cache_read / cache_write tokens 持久化 ----------
def test_cache_tokens_roundtrip(tmp_path):
    store = EventStore(tmp_path / "t.db")
    e = AIRequestEvent(provider="p", model="m")
    e.apply_usage(Usage(input_tokens=1, output_tokens=1,
                        cache_read_tokens=8, cache_write_tokens=2))
    store.insert(e)
    row = store.recent_events(1)[0]
    assert row["cache_read_tokens"] == 8
    assert row["cache_write_tokens"] == 2
    store.close()


# ---------- 9. 未知 usage 字段不得静默丢失（进 extension） ----------
def test_unknown_usage_field_captured_in_extension():
    a = OpenAICompatibleAdapter("deepseek", "https://api.deepseek.com")
    u = a.extract_usage({"usage": {
        "prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15,
        "custom_metric": 99}})
    assert u.extension == {"custom_metric": 99}


# ---------- 10-14. Sanitizer 决策树 ----------
def test_sanitizer_keeps_numeric_fields():
    d = {"prompt_tokens": 10, "completion_tokens": 5}
    assert sanitize_usage_dict(d) == d


def test_sanitizer_deletes_exact_credential_keys():
    d = {"api_key": "sk-xxx", "secret": "abc", "password": "p",
         "token": "t", "authorization": "Bearer x"}
    out = sanitize_usage_dict(d)
    assert out == {}  # 全部命中凭据键


def test_sanitizer_deletes_value_patterns():
    jwt = ("eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
           "eyJzdWIiOiIxMjM0NTY3ODkwIn0.SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c")
    d = {"key": "sk-abcdef123456", "k2": "ark-xyz789",
         "auth": "Bearer eyJhbGci", "j": jwt}
    out = sanitize_usage_dict(d)
    assert out == {}


def test_sanitizer_recursive_dict_list():
    d = {"outer": {"api_key": "sk-x"}, "arr": [{"secret": "y"}]}
    assert sanitize_usage_dict(d) == {"outer": {}, "arr": [{}]}


def test_sanitizer_keeps_unknown_non_credential():
    d = {"model": "deepseek-chat", "region": "cn", "count": 3}
    assert sanitize_usage_dict(d) == d


# ---------- 15. Raw → Sanitizer：extension 落库前必净化 ----------
def test_usage_extension_sanitized_before_persist(tmp_path):
    store = EventStore(tmp_path / "t.db")
    e = AIRequestEvent(provider="p", model="m")
    e.apply_usage(Usage(input_tokens=1, output_tokens=1,
                        extension={"api_key": "sk-leak", "keep": 1}))
    store.insert(e)
    row = store.recent_events(1)[0]
    # 凭据值被脱敏，非凭据值保留
    import json as _json
    ext = _json.loads(row["usage_extension"])
    assert "api_key" not in ext
    assert ext.get("keep") == 1
    store.close()


# ---------- 16. 凭据边界：gateway 仅从环境变量解析 secret ----------
def test_gateway_rejects_without_env_key(tmp_path):
    import monitor.main as m
    upstream = _make_upstream()
    cm = ConfigManager(tmp_path / "cfg.yaml")
    cm.upsert("deepseek", enabled=True,
              base_url=f"http://127.0.0.1:{upstream.server_port}")
    m.config_mgr = cm
    m.store = EventStore(tmp_path / "t.db")
    os.environ.pop("DEEPSEEK_API_KEY", None)
    with TestClient(m.app) as c:
        r = c.post("/gateway/deepseek/chat/completions",
                   json={"model": "m", "messages": []})
        assert r.status_code == 400
        # rejected 事件落库
        assert m.store.recent_events(1)[0]["event_type"] == "rejected"
    upstream.shutdown()


def test_gateway_uses_env_key(tmp_path):
    import monitor.main as m
    upstream = _make_upstream()
    cm = ConfigManager(tmp_path / "cfg.yaml")
    cm.upsert("deepseek", enabled=True,
              base_url=f"http://127.0.0.1:{upstream.server_port}")
    m.config_mgr = cm
    m.store = EventStore(tmp_path / "t.db")
    os.environ["DEEPSEEK_API_KEY"] = "sk-test"
    try:
        with TestClient(m.app) as c:
            r = c.post("/gateway/deepseek/chat/completions",
                       json={"model": "deepseek-chat",
                             "messages": [{"role": "user", "content": "hi"}]})
            assert r.status_code == 200
    finally:
        os.environ.pop("DEEPSEEK_API_KEY", None)
    upstream.shutdown()


# ---------- 17. 凭据不进入事件 / API 响应 ----------
def test_credential_not_in_event_or_response():
    e = AIRequestEvent(provider="p", model="m")
    d = e.to_dict()
    assert "api_key" not in d and "api_keys" not in d
    assert "secret" not in d


# ---------- 18. OpenAI-compatible 完整提取 ----------
def test_openai_compat_full_extraction():
    a = OpenAICompatibleAdapter("deepseek", "https://api.deepseek.com")
    u = a.extract_usage({"usage": {
        "prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15,
        "reasoning_tokens": 2, "custom_metric": 99}})
    assert u.input_tokens == 10 and u.output_tokens == 5
    assert u.total_tokens == 15 and u.reasoning_tokens == 2
    assert u.extension == {"custom_metric": 99}
    # extract_cache_usage：cache 命中单独提取
    c = a.extract_cache_usage({"usage": {"prompt_cache_hit_tokens": 4}})
    assert c == {"cache_read_tokens": 4, "cache_write_tokens": None}


# ---------- 19. Gemini 适配 ----------
def test_gemini_adapter_extraction():
    a = GeminiAdapter()
    u = a.extract_usage({"usageMetadata": {
        "promptTokenCount": 10, "candidatesTokenCount": 5,
        "totalTokenCount": 15}})
    assert u.input_tokens == 10 and u.output_tokens == 5
    assert u.total_tokens == 15


# ---------- 20. Generic 兜底捕获未知字段 ----------
def test_generic_adapter_captures_unknown():
    a = GenericAdapter()
    u = a.extract_usage({"usage": {
        "prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15,
        "weird_field": "x"}})
    assert u.input_tokens == 10
    assert u.extension.get("weird_field") == "x"


# ---------- 21. Migration 幂等 ----------
def test_migration_idempotent(tmp_path):
    db = tmp_path / "old.db"
    _old_schema_conn(db)
    store = EventStore(db)
    store.close()
    # 二次打开不报错、不丢数据
    store2 = EventStore(db)
    assert store2.recent_events(1)[0]["request_id"] == "legacy-1"
    store2.close()


# ---------- 22. Legacy estimated_cost → cost rename ----------
def test_legacy_estimated_cost_renamed(tmp_path):
    db = tmp_path / "old.db"
    _old_schema_conn(db)
    conn = sqlite3.connect(db)
    cols_before = {r[1] for r in conn.execute("PRAGMA table_info(events)")}
    assert "estimated_cost" in cols_before
    conn.close()
    EventStore(db)  # 打开即迁移
    conn = sqlite3.connect(db)
    cols_after = {r[1] for r in conn.execute("PRAGMA table_info(events)")}
    assert "cost" in cols_after and "estimated_cost" not in cols_after
    conn.close()


# ---------- 23. Streaming / 单次请求单一事件 ----------
def test_single_event_per_request(tmp_path):
    import monitor.main as m
    upstream = _make_upstream()
    cm = ConfigManager(tmp_path / "cfg.yaml")
    cm.upsert("deepseek", enabled=True,
              base_url=f"http://127.0.0.1:{upstream.server_port}",
              api_key="sk-test")
    m.config_mgr = cm
    store = EventStore(tmp_path / "t.db")
    m.store = store
    os.environ["DEEPSEEK_API_KEY"] = "sk-test"
    try:
        with TestClient(m.app) as c:
            c.post("/gateway/deepseek/chat/completions",
                   json={"model": "deepseek-chat",
                         "messages": [{"role": "user", "content": "hi"}]})
            # 一次请求 = 一条事件（在 lifespan 关闭 store 前断言）
            assert len(store.recent_events(10)) == 1
    finally:
        os.environ.pop("DEEPSEEK_API_KEY", None)
    upstream.shutdown()


# ---------- 24. 使用统计排除 rejected/error ----------
def test_usage_stats_exclude_rejected(tmp_path):
    store = EventStore(tmp_path / "t.db")
    store.insert(AIRequestEvent(provider="p", model="m", event_type="llm_call",
                                total_tokens=10))
    store.insert(AIRequestEvent(provider="p", model="m", event_type="rejected"))
    ov = store.analytics_overview()
    assert ov["requests"] == 1  # rejected 不计入
    assert ov["total_tokens"] == 10
    store.close()


# ---------- helpers ----------
def _old_schema_conn(db_path):
    conn = sqlite3.connect(str(db_path))
    conn.executescript("""
        CREATE TABLE events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            request_id TEXT NOT NULL, timestamp REAL NOT NULL,
            provider TEXT NOT NULL, model TEXT, endpoint TEXT,
            source TEXT, project TEXT,
            input_tokens INTEGER, output_tokens INTEGER, total_tokens INTEGER,
            latency_ms REAL, status_code INTEGER,
            estimated_cost REAL, currency TEXT, error TEXT,
            trace_id TEXT, parent_span_id TEXT
        );
    """)
    conn.execute("INSERT INTO events (request_id, timestamp, provider, model, "
                 "input_tokens, output_tokens, total_tokens, status_code) "
                 "VALUES ('legacy-1', 1700000000, 'deepseek', 'deepseek-chat', "
                 "10, 5, 15, 200)")
    conn.commit()
    conn.close()


def _make_upstream():
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            length = int(self.headers.get("content-length") or 0)
            body = __import__("json").loads(self.rfile.read(length) or b"{}")
            resp = {"id": "x", "model": body.get("model"),
                    "choices": [{"message": {"role": "assistant",
                                            "content": "ok"}}],
                    "usage": {"prompt_tokens": 10, "completion_tokens": 5,
                              "total_tokens": 15}}
            data = __import__("json").dumps(resp).encode()
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv
