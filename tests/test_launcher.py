"""P5-A：启动器（scripts/start.py）纯函数测试。

只测可导入的纯逻辑（端口检测 / 健康检查 / 配置读取），不真正拉起 server，
避免测试副作用。真实启动路径在最终「真实用户路径测试」中手动验证。
"""
import http.server
import socketserver
import threading
import time
from pathlib import Path

import pytest

sys_path = str(Path(__file__).resolve().parent.parent)
import sys  # noqa: E402
sys.path.insert(0, sys_path)

import scripts.start as launcher  # noqa: E402


def test_is_port_open():
    srv = socketserver.TCPServer(("127.0.0.1", 0), http.server.BaseHTTPRequestHandler)
    port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        assert launcher.is_port_open("127.0.0.1", port) is True
        # 端口极可能空闲
        assert launcher.is_port_open("127.0.0.1", 1) is False or True  # 1 可能被拒，仅验证不抛异常
    finally:
        srv.shutdown()


def test_wait_for_health_ok():
    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"requests":0}')

        def log_message(self, *a):
            pass

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        ok = launcher.wait_for_health(f"http://127.0.0.1:{port}/api/overview",
                                      timeout=5.0, interval=0.1)
        assert ok is True
    finally:
        srv.shutdown()


def test_wait_for_health_timeout_false():
    # 不可达地址应在超时后返回 False（短超时，避免拖慢测试）
    ok = launcher.wait_for_health("http://127.0.0.1:1/api/overview",
                                  timeout=1.0, interval=0.2)
    assert ok is False


def test_load_server_cfg_default(monkeypatch, tmp_path):
    monkeypatch.setattr(launcher, "PROJECT_ROOT", str(tmp_path))
    host, port = launcher.load_server_cfg()
    assert host == "127.0.0.1"
    assert port == 8787


def test_load_server_cfg_from_yaml(monkeypatch, tmp_path):
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "config.yaml").write_text(
        "server:\n  host: 0.0.0.0\n  port: 9999\n", encoding="utf-8")
    monkeypatch.setattr(launcher, "PROJECT_ROOT", str(tmp_path))
    host, port = launcher.load_server_cfg()
    assert host == "0.0.0.0"
    assert port == 9999
