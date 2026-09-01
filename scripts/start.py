"""AI Resource Monitor — 极简启动入口（Problem A: 启动便利性）。

设计原则（free + lightweight + local）：
- 仅用 Python 标准库 + PyYAML（项目已依赖），不引入 Electron / 桌面壳 / 安装器。
- 重复启动保护：目标端口已监听即视为已运行，仅打开浏览器后退出，不启第二个 server。
- 健康检查：轮询 /api/overview 直到 200（超时仍尝试打开浏览器）。
- 自动打开浏览器；无 GUI / headless 环境静默降级，仅打印 URL。
- 启动过程只读 data/config.yaml 取 host/port，不修改任何数据 / DB。

用法：
    python scripts/start.py                # 启动并自动打开浏览器
    python scripts/start.py --no-browser   # 启动但不打开浏览器
    python scripts/start.py --open-only    # 仅打开已运行实例的浏览器
"""
from __future__ import annotations

import argparse
import os
import socket
import subprocess
import sys
import time

try:
    import webbrowser
except ImportError:  # pragma: no cover - 标准库始终可用
    webbrowser = None

try:
    import yaml
except ImportError:  # 退化：无 yaml 时用默认 127.0.0.1:8787
    yaml = None

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8787


def load_server_cfg() -> tuple[str, int]:
    """从 data/config.yaml 读取 server.host/port；缺省 127.0.0.1:8787。"""
    cfg_path = os.path.join(PROJECT_ROOT, "data", "config.yaml")
    host, port = DEFAULT_HOST, DEFAULT_PORT
    if yaml is not None and os.path.exists(cfg_path):
        try:
            with open(cfg_path, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
            srv = data.get("server") or {}
            host = str(srv.get("host", host))
            port = int(srv.get("port", port))
        except Exception:
            pass
    return host, port


def is_port_open(host: str, port: int, timeout: float = 0.5) -> bool:
    """端口是否已被监听（用于重复启动保护）。"""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def wait_for_health(url: str, timeout: float = 15.0, interval: float = 0.3) -> bool:
    """轮询健康检查端点，直到返回 200 或超时。"""
    try:
        import urllib.request
    except ImportError:  # pragma: no cover
        return False
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=interval) as resp:
                if resp.status == 200:
                    return True
        except Exception:
            pass
        time.sleep(interval)
    return False


def open_browser(url: str) -> bool:
    """打开默认浏览器；无 GUI 环境静默失败并返回 False。"""
    if webbrowser is None:
        return False
    try:
        return webbrowser.open(url)
    except Exception:
        return False


def start_server(host: str, port: int) -> subprocess.Popen:
    """以前台子进程启动 Monitor（python -m monitor.main）。返回 Popen。"""
    return subprocess.Popen(
        [sys.executable, "-m", "monitor.main"],
        cwd=PROJECT_ROOT,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.STDOUT,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="AI Resource Monitor launcher")
    parser.add_argument("--host", default=None, help="覆盖监听 host")
    parser.add_argument("--port", type=int, default=None, help="覆盖监听 port")
    parser.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    parser.add_argument(
        "--open-only",
        action="store_true",
        help="仅打开已运行实例的浏览器，不启动新 server",
    )
    args = parser.parse_args(argv)

    host, port = load_server_cfg()
    host = args.host or host
    port = args.port or port
    base = f"http://{host}:{port}"
    health_url = base + "/api/overview"

    if is_port_open(host, port):
        print(f"[Monitor] 已在运行：{base}")
        if not args.no_browser:
            if not open_browser(base):
                print(f"[Monitor] 无法自动打开浏览器，请手动访问：{base}")
        return 0

    if args.open_only:
        print(f"[Monitor] 端口 {port} 未监听，没有运行中的实例。")
        return 1

    print(f"[Monitor] 启动中：{base} ...")
    proc = start_server(host, port)
    try:
        if wait_for_health(health_url):
            print(f"[Monitor] 就绪：{base}")
        else:
            print(f"[Monitor] 警告：健康检查超时，但 server 进程已启动（PID {proc.pid}）。")
        if not args.no_browser:
            if not open_browser(base):
                print(f"[Monitor] 无法自动打开浏览器，请手动访问：{base}")
        proc.wait()
        return proc.returncode or 0
    except KeyboardInterrupt:
        print("\n[Monitor] 正在停止 ...")
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        return 0


if __name__ == "__main__":
    sys.exit(main())
