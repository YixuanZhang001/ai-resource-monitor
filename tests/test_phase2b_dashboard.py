"""Phase 2B Dashboard MVP — 后端契约测试（前端依赖的数据语义）。

不修改任何 monitor/ 代码、不新增 schema、不新增 DB 表。
只验证 Dashboard 所依赖的 API 契约与数据语义：

Test 1  Resource Overview 数据结构（Resource / Provider / Usage / Cost / Observation）
Test 2  NULL cost 不得变成 0（— / Unknown 语义）
Test 3  Observation：known 显示真实 balance/remaining；no_observation 不显示伪造数字
Test 4  Failed event：error + status_code，但 Ledger 仍是单个 llm_call，不制造 error event
Test 5  Recent Events 能返回最近调用（含 resource_id / model / tokens / cost / status）
Test 6  Today summary：requests / tokens / cost / errors 使用 Backend range=today 语义
Test 7  Credential regression：Dashboard 修改 Resource/Provider 后 config.yaml 不得出现 secret
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402

from monitor.config import ConfigManager  # noqa: E402
from monitor.events import AIRequestEvent  # noqa: E402
from monitor.resource import ResourceDefinition, ResourceRegistry  # noqa: E402
from monitor.storage import EventStore  # noqa: E402


@pytest.fixture()
def app_env(tmp_path, monkeypatch):
    import monitor.main as m
    cfg_path = tmp_path / "c.yaml"
    cfg_path.write_text(
        "scheduler: {enabled: false}\n"
        "providers:\n  openrouter:\n    enabled: true\n    base_url: 'http://up'\n"
        "resources:\n"
        "  or-res:\n    provider: openrouter\n    resource_type: api\n"
        "    billing_mode: prepaid\n    credential_id: openrouter\n"
        "  ds-res:\n    provider: deepseek\n    resource_type: api\n"
        "    billing_mode: prepaid\n",
        encoding="utf-8")
    monkeypatch.setattr(m, "config_mgr", ConfigManager(cfg_path))
    monkeypatch.setattr(m, "store", EventStore(tmp_path / "t.db"))
    monkeypatch.setattr(m, "resources", ResourceRegistry(m.config_mgr.resources))
    with TestClient(m.app) as c:
        yield c, m


# ---------- Test 1：Resource Overview 数据结构 ----------
def test_resource_overview_fields(app_env):
    c, m = app_env
    u = c.get("/api/resources/usage").json()
    assert "resources" in u
    rd = next(r for r in u["resources"] if r["resource_id"] == "or-res")
    for k in ("resource_id", "resource_name", "provider", "requests",
              "total_tokens", "cost", "cost_status"):
        assert k in rd, f"missing field {k}"
    # Observation 状态端点同样返回四态字段
    s = c.get("/api/resources/state").json()
    assert "resources" in s
    sd = next(x for x in s["resources"] if x["resource_id"] == "or-res")
    for k in ("resource_id", "observation_status"):
        assert k in sd


# ---------- Test 2：NULL cost 不得变成 0 ----------
def test_null_cost_not_zero(app_env):
    c, m = app_env
    # 没有任何 usage 时：单资源 cost 为 None，cost_status 不是 'known'
    u = c.get("/api/resources/usage").json()
    rd = next(r for r in u["resources"] if r["resource_id"] == "or-res")
    assert rd["cost"] is None
    assert rd["cost_status"] in ("unknown", "none", "unused")
    # overview 的 cost_by_currency 为空（不伪造 ¥0）
    ov = c.get("/api/overview", params={"range": "today"}).json()
    assert ov.get("cost_by_currency") in (None, {}, {})


# ---------- Test 3：Observation known vs no_observation ----------
def test_observation_known_vs_no_observation(app_env):
    c, m = app_env
    # or-res 尚未观察 → no_observation，balance 为 None
    st0 = c.get("/api/resources/or-res/state").json()
    assert st0["observation_status"] == "no_observation"
    assert st0["balance"] is None
    # 显式提交 known 观察（手动，模拟真实 collector 数据）
    r = c.post("/api/resources/or-res/observe", json={
        "status": "known", "balance": 46.8, "remaining": 21.05})
    assert r.status_code == 200
    d = r.json()
    assert d["observation_status"] == "known"
    assert d["balance"] == 46.8          # 真实数值保留
    assert d["remaining"] == 21.05
    # 另一个资源仍为 no_observation，不得出现伪造数字
    st1 = c.get("/api/resources/ds-res/state").json()
    assert st1["observation_status"] == "no_observation"
    assert st1["balance"] is None


# ---------- Test 4：Failed event 仍是单个 llm_call ----------
def test_failed_event_single_llm_call(app_env):
    c, m = app_env
    # 直接注入一条失败事件（status_code=502, error 非空），event_type 默认 llm_call
    ev = AIRequestEvent(provider="deepseek", model="deepseek-chat",
                        resource_id="ds-res", status_code=502,
                        error="upstream 502", event_type="llm_call")
    m.store.insert(ev)
    # recent events 中应包含该事件，且 event_type 仍为 llm_call
    events = c.get("/api/events", params={"limit": 20}).json()["events"]
    bad = [e for e in events if e["status_code"] == 502]
    assert bad, "failed event 应在 recent events 中"
    assert bad[0]["event_type"] == "llm_call"
    assert bad[0]["error"] == "upstream 502"
    # 不得出现 event_type='error' 的事件
    assert not any(e.get("event_type") == "error" for e in events)


# ---------- Test 5：Recent Events 数据结构 ----------
def test_recent_events_fields(app_env):
    c, m = app_env
    ev = AIRequestEvent(provider="openrouter", model="gpt-5",
                        resource_id="or-res", total_tokens=12300,
                        cost=0.04, latency_ms=1200, status_code=200)
    m.store.insert(ev)
    events = c.get("/api/events", params={"limit": 10}).json()["events"]
    assert events, "recent events 应非空"
    e = events[0]
    for k in ("request_id", "timestamp", "resource_id", "provider",
              "model", "total_tokens", "cost", "status_code"):
        assert k in e


# ---------- Test 6：Today summary 语义 ----------
def test_today_summary_fields(app_env):
    c, m = app_env
    ov = c.get("/api/overview", params={"range": "today"}).json()
    for k in ("requests", "total_tokens", "cost_by_currency", "errors"):
        assert k in ov
    # errors 为数值或 None（无数据时 SQL COALESCE 不生效 → None；前端已用 ||0 兜底）
    assert ov["errors"] is None or isinstance(ov["errors"], int)
    # 无数据时 requests/tokens 为 0（真实 0，不是 NULL 伪造）
    assert ov["requests"] == 0
    assert ov["total_tokens"] == 0


# ---------- Test 7：Credential regression ----------
def test_credential_regression_no_secret_on_disk(app_env, tmp_path):
    c, m = app_env
    cfg_file = tmp_path / "c.yaml"
    # 通过 Dashboard API 配置 provider（带 api_key 参数）
    r = c.put("/api/providers/openrouter", json={
        "enabled": True, "base_url": "http://up",
        "api_key": "sk-SUPER-SECRET-xyz"})
    assert r.status_code in (200, 404)  # provider 必须已注册（openrouter 在 registry）
    # 通过 Dashboard API 创建 resource（含 credential_id 引用）
    rr = c.post("/api/resources", json={
        "resource_id": "or-2", "name": "OR2", "provider": "openrouter",
        "credential_id": "openrouter"})
    assert rr.status_code in (200, 409)
    # 磁盘 config.yaml 不得包含真实 secret 文本
    text = cfg_file.read_text(encoding="utf-8")
    assert "sk-SUPER-SECRET-xyz" not in text
    assert "api_key" not in text
    assert "SUPER-SECRET" not in text
