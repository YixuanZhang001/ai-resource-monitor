"""Test isolation（P6 审计修复：hermetic，不依赖生产 data/）。

两个隔离面：

1) 数据目录
   monitor.main 在 import 期构造 EventStore(DATA_DIR/"monitor.db")。
   把 DATA_DIR 指向临时目录，测试套件永不打开/迁移生产库。

2) 配置基线
   旧实现只重定向了目录，却留下一个空目录 —— 导致 import 期的
   config_mgr 被"饿死"成空配置，凡是不自行 monkeypatch config_mgr 的测试
   （如 test_credential_access）就会失败；上一轮的临时绕法是"复制生产
   data/config.yaml"，那等于让测试依赖生产数据，不是真正的隔离。
   这里显式播种一份**自包含**的测试配置，默认值与出厂配置一致。
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


def _bootstrap():
    data_dir = os.environ.get("MONITOR_DATA_DIR")
    if not data_dir:
        data_dir = tempfile.mkdtemp(prefix="arm_test_")
        os.environ["MONITOR_DATA_DIR"] = data_dir
    cfg = Path(data_dir) / "config.yaml"
    if not cfg.exists():
        cfg.write_text(_TEST_CONFIG, encoding="utf-8")
    return data_dir


_bootstrap()
