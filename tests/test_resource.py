"""P0-2 测试：Resource Registry + Event resource_id 链路。"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from monitor.config import ConfigManager  # noqa: E402
from monitor.resource import ResourceDefinition, ResourceRegistry  # noqa: E402


def _registry():
    return ResourceRegistry({
        "deepseek-free": ResourceDefinition(
            resource_id="deepseek-free", name="DeepSeek Free",
            provider="deepseek", resource_type="quota",
            billing_mode="free", account_scope="account_a"),
        "deepseek-paid": ResourceDefinition(
            resource_id="deepseek-paid", name="DeepSeek Paid",
            provider="deepseek", resource_type="api",
            billing_mode="prepaid", account_scope="account_a"),
        "disabled-res": ResourceDefinition(
            resource_id="disabled-res", enabled=False),
    })


# ---------- Resource Registry ----------

def test_resource_exists():
    r = _registry()
    assert r.exists("deepseek-paid") is True
    assert r.exists("does-not-exist") is False


def test_resource_get():
    r = _registry()
    d = r.get("deepseek-free")
    assert d is not None and d.provider == "deepseek"
    assert d.resource_type == "quota" and d.billing_mode == "free"
    assert r.get("nope") is None


def test_resource_list_enabled_only():
    r = _registry()
    ids = [d.resource_id for d in r.list()]
    assert ids == ["deepseek-free", "deepseek-paid"]   # disabled 排除，按 id 排序
    assert len(r.list(enabled_only=False)) == 3


# ---------- Config Loading ----------

def test_config_loads_resources(tmp_path):
    cfg_path = tmp_path / "c.yaml"
    cfg_path.write_text(
        "resources:\n"
        "  deepseek-free:\n"
        "    name: DeepSeek Free\n    provider: deepseek\n"
        "    resource_type: quota\n    billing_mode: free\n"
        "    account_scope: account_a\n",
        encoding="utf-8")
    cfg = ConfigManager(cfg_path)
    assert "deepseek-free" in cfg.resources
    d = cfg.resources["deepseek-free"]
    assert d.provider == "deepseek" and d.resource_type == "quota"
    assert d.billing_mode == "free" and d.account_scope == "account_a"
    assert d.enabled is True   # 默认启用
    # 空 resources → 空 dict（兼容旧配置）
    cfg2 = ConfigManager(tmp_path / "empty.yaml")
    assert cfg2.resources == {}


def test_resource_definition_never_holds_api_key():
    """Resource Definition 不含任何 sk- 凭证（account_scope 只是逻辑标识）。"""
    r = _registry()
    blob = str([d.__dict__ for d in r.list(enabled_only=False)])
    assert "sk-" not in blob and "api_keys" not in blob
