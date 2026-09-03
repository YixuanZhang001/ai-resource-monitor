"""P6 收尾回归：Resource 状态语义分离（observation failure ≠ Resource ERROR）。

核心问题（用户真实可见）：Dashboard Resource 卡片曾把 Monitor 自己的 observation
failure（如 scheduler 因 credential unavailable 每 tick 写 status='error'，monitor 停止后
冻结）误当成 Resource 本身 ERROR 展示。

本轮修复：
- /api/resources/state 与 /api/resources/{id}/state 为每个 resource 附加
  authoritative Resource Health（基于真实请求 events）+ stale 标记（observation 过期）。
- Dashboard 卡片主状态改用 Resource Health，Observation 降为带 OBS: 前缀的副状态。

本文件只验证语义分离，不触碰生产 DB（conftest 已把 MONITOR_DATA_DIR 指向临时目录，
且本 fixture 再用 tmp_path 重建 store/config）。

覆盖 brief 要求的 5 类：
1. observation credential failure 不被当成 Resource health failure
2. Resource 实际请求正常 + observation failure 时，API 能区分二者
3. stale observation 不会永久伪装成当前 Resource ERROR
4. 真正的 Resource request failure 仍能被识别
5. observation 四态契约（known/unavailable/error/no_observation）不被破坏（P6 不回归）
"""
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from monitor.events import AIRequestEvent
from monitor.observe import ResourceObservation
from monitor.resource import ResourceRegistry
from monitor.storage import EventStore
from monitor.config import ConfigManager

CFG = (
    "scheduler:\n"
    "  enabled: false\n"
    "  interval_seconds: 300\n"          # stale 阈值 = 2×300 = 600s
    "providers:\n"
    "  deepseek:\n"
    "    enabled: true\n"
    "    base_url: 'http://up'\n"
    "resources:\n"
    "  sem-res:\n"
    "    provider: deepseek\n"
    "    resource_type: api\n"
    "    billing_mode: prepaid\n"
)


@pytest.fixture()
def app(tmp_path, monkeypatch):
    import monitor.main as m
    cfg_path = tmp_path / "c.yaml"
    cfg_path.write_text(CFG, encoding="utf-8")
    monkeypatch.setattr(m, "config_mgr", ConfigManager(cfg_path))
    monkeypatch.setattr(m, "store", EventStore(tmp_path / "t.db"))
    monkeypatch.setattr(m, "resources", ResourceRegistry(m.config_mgr.resources))
    m.store.migrate()
    with TestClient(m.app) as c:
        yield c, m


def _events(m, resource_id, status_code=200, n=10, age=100.0):
    """插入 n 条 llm_call 事件（age 秒前，确保 health 新鲜）。"""
    base = time.time() - age
    for i in range(n):
        e = AIRequestEvent(
            provider="deepseek", model="deepseek-chat",
            total_tokens=10, status_code=status_code,
            resource_id=resource_id, timestamp=base - i)
        if status_code >= 400:
            e.error = "boom"
        m.store.insert(e)


def _observe(m, resource_id, status="error", observed_at=None, error="credential unavailable",
             metadata=None, balance=None):
    m.store.insert_observation(ResourceObservation(
        resource_id=resource_id, status=status, source="scheduler",
        error=error, balance=balance, metadata=metadata,
        observed_at=observed_at if observed_at is not None else time.time()))


def _state_for(c, resource_id):
    d = c.get("/api/resources/state").json()
    by = {s["resource_id"]: s for s in d["resources"] + d.get("unregistered", [])}
    return by.get(resource_id)


# 1. observation credential failure 不被当成 Resource health failure
def test_observation_credential_failure_not_health_failure(app):
    c, m = app
    _events(m, "sem-res", status_code=200, n=10)          # Resource 真实健康
    _observe(m, "sem-res", status="error",
             observed_at=time.time() - 10)                # 新鲜 observation error
    s = _state_for(c, "sem-res")
    assert s["health"]["health"] == "healthy", "credential failure 不能污染 Resource Health"
    assert s["observation_status"] == "error"
    assert s["stale"] is False, "新鲜 observation 不应 stale"


# 2. Resource 实际请求正常 + observation failure → 二者在 API 中明确区分
def test_healthy_plus_observation_error_distinguished(app):
    c, m = app
    _events(m, "sem-res", status_code=200, n=10)
    _observe(m, "sem-res", status="error", observed_at=time.time() - 10)
    s = _state_for(c, "sem-res")
    # 两个维度独立存在、互不等价
    assert "health" in s and "observation_status" in s
    assert s["health"]["health"] == "healthy"
    assert s["health"]["requests"] == 10
    assert s["health"]["errors"] == 0
    # observation 反映的是 Monitor 观察能力，不是 Resource 本身
    assert s["observation_status"] == "error"
    assert s["health"]["health"] != s["observation_status"]


# 3. stale observation 不会永久伪装成当前 Resource ERROR
def test_stale_observation_not_masquerade_as_resource_error(app):
    c, m = app
    _events(m, "sem-res", status_code=200, n=10)          # Resource 仍健康
    _observe(m, "sem-res", status="error",
             observed_at=time.time() - 1000)              # > 2×interval(600) → stale
    s = _state_for(c, "sem-res")
    assert s["stale"] is True, "过期 observation 必须标记为 stale"
    assert s["observation_status"] == "error"
    # 即使 observation 冻结为 error，Resource Health 仍来自真实请求 → healthy
    assert s["health"]["health"] == "healthy"
    # 单资源端点同样一致
    one = c.get("/api/resources/sem-res/state").json()
    assert one["stale"] is True
    assert one["health"]["health"] == "healthy"


# 4. 真正的 Resource request failure 仍能被识别
def test_real_request_failure_identifiable(app):
    c, m = app
    _events(m, "sem-res", status_code=500, n=5)            # 全部真实失败
    s = _state_for(c, "sem-res")
    assert s["health"]["health"] == "unavailable", "全部请求失败 → unavailable"
    assert s["health"]["requests"] == 5
    assert s["health"]["errors"] == 5
    assert s["health"]["error_rate"] == 1.0


# 5. observation 四态契约不被破坏（P6 不回归）
def test_observation_four_state_contract_preserved(app):
    c, m = app
    _events(m, "sem-res", status_code=200, n=3)
    # known（新鲜）
    _observe(m, "sem-res", status="known", observed_at=time.time() - 5)
    s = _state_for(c, "sem-res")
    assert s["observation_status"] == "known" and s["stale"] is False
    # unavailable（新鲜）
    _observe(m, "sem-res", status="unavailable", observed_at=time.time() - 5)
    s = _state_for(c, "sem-res")
    assert s["observation_status"] == "unavailable"
    # error（新鲜）
    _observe(m, "sem-res", status="error", observed_at=time.time() - 5, error="x")
    s = _state_for(c, "sem-res")
    assert s["observation_status"] == "error" and s["stale"] is False
    # no_observation：无快照的 resource 必现
    d = c.get("/api/resources/state").json()
    assert all("health" in r for r in d["resources"]), "每个 resource 都应带 health"
    assert all("stale" in r for r in d["resources"]), "每个 resource 都应带 stale"


# 6. 余额币种来自 observation metadata（面板要显示"剩余额度"，必须知道币种）
def test_balance_currency_from_metadata(app):
    c, m = app
    _observe(m, "sem-res", status="known", error=None, balance=45.93,
             metadata={"provider": "deepseek", "currency": "CNY",
                       "is_available": True})
    s = _state_for(c, "sem-res")
    assert s["balance"] == 45.93
    assert s["currency"] == "CNY"


# 7. 无 metadata → currency=None：绝不按 provider 猜测币种（跨币种不可求和）
def test_balance_currency_none_when_metadata_missing(app):
    c, m = app
    _observe(m, "sem-res", status="known", error=None, balance=10.0)
    s = _state_for(c, "sem-res")
    assert s["balance"] == 10.0
    assert s["currency"] is None, "无 metadata 时不得猜测币种"
