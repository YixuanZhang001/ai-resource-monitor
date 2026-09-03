"""Test isolation（P6 审计修复：hermetic，不依赖生产 data/）。

三个隔离面：

1) 数据目录
   monitor.main 在 import 期构造 EventStore(DATA_DIR/"monitor.db")。
   把 DATA_DIR 指向临时目录，测试套件永不打开/迁移生产库。

2) 配置基线
   旧实现只重定向了目录，却留下一个空目录 —— 导致 import 期的
   config_mgr 被"饿死"成空配置，凡是不自行 monkeypatch config_mgr 的测试
   （如 test_credential_access）就会失败；上一轮的临时绕法是"复制生产
   data/config.yaml"，那等于让测试依赖生产数据，不是真正的隔离。
   这里显式播种一份**自包含**的测试配置，默认值与出厂配置一致。

3) 网络出口（loopback 必须直连）
   e2e 测试只与本机 127.0.0.1 上临时起的 uvicorn / 假上游通信。若从环境继承了
   HTTP_PROXY / HTTPS_PROXY，httpx（测试客户端与 gateway 内部的 AsyncClient）
   会把 loopback 流量也绕经代理；代理在**复用连接**上会把请求行按 absolute-form
   （GET http://127.0.0.1:port/path）转发，Starlette 因此匹配不到任何路由，
   返回 404 —— 表现为「同一 Client 第一次请求成功、之后全部 404」。
   这与被测代码无关，纯属测试环境泄漏，故在此显式让 loopback 绕过代理。
"""
import os
import tempfile
from pathlib import Path

_TEST_CONFIG = """\
# 测试基线配置（tests/conftest.py 生成；不读生产 data/config.yaml）
server:
  host: 127.0.0.1
  port: 8787
providers:
  openai:
    enabled: false
    base_url: https://api.openai.com
    test_model: gpt-4o-mini
  deepseek:
    enabled: true
    base_url: https://api.deepseek.com
    test_model: deepseek-chat
  openrouter:
    enabled: false
    base_url: https://openrouter.ai/api/v1
    test_model: openai/gpt-4o-mini
  gemini:
    enabled: false
    base_url: https://generativelanguage.googleapis.com
    test_model: gemini-2.5-flash
scheduler:
  enabled: false
  interval_seconds: 9999
# 与出厂配置同构：deepseek 下 2 个 enabled 资源 —— 无显式 header 时
# 「唯一资源兜底」不成立，归因应为 NULL（test_credential_access 依赖此前提）
resources:
  deepseek-paid:
    name: DeepSeek Paid
    provider: deepseek
    resource_type: api
    billing_mode: prepaid
  deepseek-free:
    name: DeepSeek Free
    provider: deepseek
    resource_type: quota
    billing_mode: free
    enabled: false
  t-r:
    name: T
    provider: deepseek
"""


_LOOPBACK = ("127.0.0.1", "localhost", "::1")


def _bypass_proxy_for_loopback():
    """把 loopback 写进 no_proxy，保证测试流量不经任何继承来的 HTTP 代理。"""
    for var in ("no_proxy", "NO_PROXY"):
        current = [x.strip() for x in os.environ.get(var, "").split(",") if x.strip()]
        for host in _LOOPBACK:
            if host not in current:
                current.append(host)
        os.environ[var] = ",".join(current)


def _bootstrap():
    _bypass_proxy_for_loopback()
    data_dir = os.environ.get("MONITOR_DATA_DIR")
    if not data_dir:
        data_dir = tempfile.mkdtemp(prefix="arm_test_")
        os.environ["MONITOR_DATA_DIR"] = data_dir
    cfg = Path(data_dir) / "config.yaml"
    if not cfg.exists():
        cfg.write_text(_TEST_CONFIG, encoding="utf-8")
    return data_dir


_bootstrap()
