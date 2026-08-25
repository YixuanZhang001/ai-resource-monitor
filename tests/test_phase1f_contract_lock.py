"""Phase 1F-B — Ledger Contract Lock。

目标：用最小新增测试把当前已实现（且已 GO）的 Ledger 语义正式锁死，
防止后续 Resource Observation / Dashboard 开发时发生回归。

锁定的契约（来自 Phase 1E Frozen Contract，已在 dc00d58 验证）：
- 一个真实逻辑调用 = 一个 Ledger Event（失败路径也不例外）
- 失败事件 event_type 仍是 "llm_call"（绝不发射 event_type="error"）
- Billing 三列（billing_status / list_cost / pricing_snapshot_id）在没有真实
  billing 数据源时保持 NULL，不伪造 "unknown"/0/"free"/假 snapshot。

本文件不修改任何生产代码、不新增字段/表、不实现 Billing / error event /
GenericAdapter 接线。
"""
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402

from monitor.config import ConfigManager  # noqa: E402
from monitor.main import app as _app  # noqa: E402
from monitor.storage import EventStore  # noqa: E402

_ENV_KEY = "DEEPSEEK_API_KEY"


def _make_upstream(status: int = 200) -> ThreadingHTTPServer:
    """返回可配置状态码的上游 stub。200 时返回标准 OpenAI 风格 usage。"""

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            n = int(self.headers.get("content-length") or 0)
            body = json.loads(self.rfile.read(n) or b"{}")
            if status == 200:
                resp = {
                    "id": "x", "model": body.get("model"),
                    "choices": [{"message": {"role": "assistant",
                                            "content": "ok"}}],
                    "usage": {"prompt_tokens": 10, "completion_tokens": 5,
                              "total_tokens": 15},
                }
                data = json.dumps(resp).encode()
                self.send_response(200)
            else:
                data = json.dumps({"error": {"message": "boom"}}).encode()
                self.send_response(status)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def _setup(port: int, tmp_path: Path) -> EventStore:
    """复刻既有测试 wiring：替换 module-global config_mgr / store，
    lifespan 会据此重建 gateway.init 绑定到本测试的 store。"""
    import monitor.main as m
    cm = ConfigManager(tmp_path / "cfg.yaml")
    # 注意：upsert 不持久化 api_key（Phase 1E-C 安全边界）；key 走环境变量
    cm.upsert("deepseek", enabled=True,
              base_url=f"http://127.0.0.1:{port}")
    m.config_mgr = cm
    store = EventStore(tmp_path / "t.db")
    m.store = store
    os.environ[_ENV_KEY] = "sk-test"
    return store


def _teardown(up: ThreadingHTTPServer):
    os.environ.pop(_ENV_KEY, None)
    try:
        up.shutdown()
        up.server_close()  # 释放监听端口，避免端口残留导致后续连接挂起
    except Exception:
        pass


# ---------- Invariant 1a：upstream 500 → 恰好一个 llm_call Event ----------
def test_failed_upstream_500_single_llm_call_event(tmp_path: Path):
    import monitor.main as m
    up = _make_upstream(status=500)
    store = _setup(up.server_port, tmp_path)
    try:
        with TestClient(m.app) as c:
            c.post("/gateway/deepseek/chat/completions",
                   json={"model": "deepseek-chat",
                         "messages": [{"role": "user", "content": "hi"}]})
            rows = store.recent_events(20)
            # 一个逻辑调用 = 恰好一个 Event，绝不因 error 再生成第二条
            assert len(rows) == 1, [r["event_type"] for r in rows]
            ev = rows[0]
            assert ev["event_type"] == "llm_call"
            assert ev["event_type"] != "error"
            assert ev["error"] is not None          # 失败信息被记录
            assert ev["status_code"] is not None and ev["status_code"] >= 400
    finally:
        _teardown(up)


# ---------- Invariant 1b：连接失败 / timeout → 恰好一个 llm_call Event ----------
def test_failed_connection_single_llm_call_event(tmp_path: Path):
    import monitor.main as m
    up = _make_upstream(status=200)
    port = up.server_port
    # 关闭端口并释放 socket → httpx 连接立即被拒绝（等价于 timeout/connection error）
    up.shutdown()
    up.server_close()
    store = _setup(port, tmp_path)
    try:
        with TestClient(m.app) as c:
            c.post("/gateway/deepseek/chat/completions",
                   json={"model": "deepseek-chat",
                         "messages": [{"role": "user", "content": "hi"}]})
            rows = store.recent_events(20)
            assert len(rows) == 1, [r["event_type"] for r in rows]
            ev = rows[0]
            assert ev["event_type"] == "llm_call"
            assert ev["error"] is not None
            assert ev["status_code"] is not None and ev["status_code"] >= 400
    finally:
        _teardown(up)


# ---------- Invariant 2：生产代码不得发射 event_type="error" ----------
def test_failure_never_emits_error_event_type(tmp_path: Path):
    """锁死：任何失败调用都不得产生 event_type == "error" 的 Ledger 行。

    区分两层概念：
    - SSE broadcast kind="error"（gateway._publish）是实时流广播类型，
      不是 Event Ledger 的 event_type。
    - 本测试针对数据库 Event Ledger 的 event_type 列。
    """
    import monitor.main as m
    up = _make_upstream(status=500)
    store = _setup(up.server_port, tmp_path)
    try:
        with TestClient(m.app) as c:
            c.post("/gateway/deepseek/chat/completions",
                   json={"model": "deepseek-chat",
                         "messages": [{"role": "user", "content": "hi"}]})
            rows = store.recent_events(20)
            # 绝不出现 event_type == "error"
            assert [r for r in rows if r["event_type"] == "error"] == []
            # 失败事件本身仍是 llm_call（且带 error 字段）
            assert any(r["event_type"] == "llm_call" and r["error"]
                       for r in rows)
    finally:
        _teardown(up)


# ---------- Invariant 3：Billing 空轴在无真实 source 时保持 NULL ----------
def test_billing_axis_null_without_source(tmp_path: Path):
    """锁死：当前 pipeline 没有真实 billing 数据源，三列必须为 NULL。

    测试目的不是验证 gateway 成功，而是验证 billing 列：
    - 不为 "unknown"
    - 不为 0
    - 不为 "free"
    - 不为任何伪造的 pricing_snapshot_id
    """
    import monitor.main as m
    up = _make_upstream(status=200)
    store = _setup(up.server_port, tmp_path)
    try:
        with TestClient(m.app) as c:
            c.post("/gateway/deepseek/chat/completions",
                   json={"model": "deepseek-chat",
                         "messages": [{"role": "user", "content": "hi"}]})
            rows = store.recent_events(20)
            assert len(rows) == 1
            ev = rows[0]
            assert ev["event_type"] == "llm_call"
            # 无真实 billing 数据源 → 保持 NULL，不伪造
            assert ev["billing_status"] is None
            assert ev["list_cost"] is None
            assert ev["pricing_snapshot_id"] is None
    finally:
        _teardown(up)
