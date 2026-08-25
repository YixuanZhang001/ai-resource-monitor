"""P0-7 测试：Resource Observation Framework。

覆盖：migration 幂等 / 模型 / 四态 / NULL≠0 / 无 observation 仍出现 / 最新 /
历史保留 / 删除保留 / UNREGISTERED / 404 / Collector→Storage 闭环 / analytics 不受影响。
"""
import sqlite3
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402

from monitor.config import ConfigManager  # noqa: E402
from monitor.observe import ManualObservationCollector, ResourceObservation  # noqa: E402
from monitor.resource import ResourceDefinition, ResourceRegistry  # noqa: E402
from monitor.storage import EventStore  # noqa: E402

CFG = (
    "scheduler: {enabled: false}\n"
    "providers:\n"
    "  deepseek:\n    enabled: true\n    base_url: 'http://up'\n"
    "    api_keys:\n    - sk-test\n"
    "resources:\n"
    "  obs-paid:\n    provider: deepseek\n    resource_type: api\n"
    "    billing_mode: prepaid\n"
    "  obs-free:\n    provider: deepseek\n    resource_type: quota\n"
    "    billing_mode: free\n"
)


@pytest.fixture()
def app(tmp_path, monkeypatch):
    import monitor.main as m

    cfg_path = tmp_path / "c.yaml"
    cfg_path.write_text(CFG, encoding="utf-8")
    monkeypatch.setattr(m, "config_mgr", ConfigManager(cfg_path))
    monkeypatch.setattr(m, "store", EventStore(tmp_path / "t.db"))
    monkeypatch.setattr(m, "resources", ResourceRegistry(m.config_mgr.resources))
    with TestClient(m.app) as c:
        yield c, m


def _obs(resource_id="obs-paid", status="known", balance=100.0, quota=1000.0,
         remaining=900.0, observed_at=None, source="manual"):
    return ResourceObservation(
        resource_id=resource_id, status=status, balance=balance, quota=quota,
        remaining=remaining, source=source,
        observed_at=observed_at or time.time())


# ---------- 1. migration 幂等 ----------

def test_migration_idempotent(tmp_path):
    from scripts.migrate import migrate  # noqa: E402
    db = tmp_path / "old.db"
    conn = sqlite3.connect(db)
    # 旧 schema：只有 events 表（无 resource_states）
    conn.execute("CREATE TABLE events (id INTEGER PRIMARY KEY, request_id TEXT,"
                 " timestamp REAL, provider TEXT, model TEXT, source TEXT,"
                 " project TEXT, status_code INTEGER)")
    conn.commit()
    conn.close()
    r1 = migrate(db)
    assert "resource_states" in r1["tables"]
    r2 = migrate(db)
    assert "resource_states" not in r2["tables"]   # 二次无新建
    conn = sqlite3.connect(db)
    cols = [r[1] for r in conn.execute("PRAGMA table_info(resource_states)")]
    assert {"resource_id", "observed_at", "status", "balance", "quota",
            "remaining", "reset_at", "expires_at", "source", "error",
            "metadata"} <= set(cols)
    conn.close()


# ---------- 2. 模型 ----------

def test_resource_observation_model():
    o = ResourceObservation(resource_id="r1")
    assert o.status == "no_observation" and o.balance is None
    assert o.remaining is None                    # NULL ≠ 0
    o2 = ResourceObservation(resource_id="r1", status="known", balance=0)
    assert o2.balance == 0                        # 0 是真实的 0


def test_manual_collector_forces_null_on_non_known():
    c = ManualObservationCollector({"status": "unavailable", "balance": 999})
    o = c.observe(ResourceDefinition(resource_id="r1"))
    assert o.status == "unavailable" and o.balance is None
    c2 = ManualObservationCollector({"status": "wat"})
    with pytest.raises(ValueError):
        c2.observe(ResourceDefinition(resource_id="r1"))


# ---------- 3. 四态 API ----------

def test_no_observation_state(app):
    c, m = app
    r = c.get("/api/resources/obs-paid/state")
    assert r.status_code == 200
    d = r.json()
    assert d["observation_status"] == "no_observation"
    assert d["balance"] is None and d["remaining"] is None   # NULL ≠ 0


def test_known_observation(app):
    c, m = app
    r = c.post("/api/resources/obs-paid/observe",
               json={"status": "known", "balance": 100, "quota": 1000,
                     "remaining": 900})
    assert r.status_code == 200
    d = c.get("/api/resources/obs-paid/state").json()
    assert d["observation_status"] == "known"
    assert d["balance"] == 100 and d["quota"] == 1000 and d["remaining"] == 900


def test_known_zero_balance_is_zero(app):
    c, m = app
    c.post("/api/resources/obs-paid/observe",
           json={"status": "known", "balance": 0})
    d = c.get("/api/resources/obs-paid/state").json()
    assert d["observation_status"] == "known"
    assert d["balance"] == 0          # 0 ≠ N/A


def test_unavailable_observation(app):
    c, m = app
    c.post("/api/resources/obs-paid/observe", json={"status": "unavailable"})
    d = c.get("/api/resources/obs-paid/state").json()
    assert d["observation_status"] == "unavailable"
    assert d["balance"] is None and d["remaining"] is None


def test_error_observation(app):
    c, m = app
    c.post("/api/resources/obs-paid/observe",
           json={"status": "error", "error": "observation failed"})
    d = c.get("/api/resources/obs-paid/state").json()
    assert d["observation_status"] == "error"
    assert d["error"] == "observation failed"


def test_error_requires_message(app):
    c, m = app
    assert c.post("/api/resources/obs-paid/observe",
                  json={"status": "error"}).status_code == 422


def test_invalid_status_rejected(app):
    c, m = app
    assert c.post("/api/resources/obs-paid/observe",
                  json={"status": "no_observation"}).status_code == 422
    assert c.post("/api/resources/obs-paid/observe",
                  json={"status": "wat"}).status_code == 422


# ---------- 4. 列表 / 最新 / 历史 ----------

def test_resource_without_observation_still_in_list(app):
    c, m = app
    d = c.get("/api/resources/state").json()
    ids = {r["resource_id"]: r for r in d["resources"]}
    assert "obs-paid" in ids and "obs-free" in ids     # 无 observation 也出现
    assert ids["obs-paid"]["observation_status"] == "no_observation"


def test_latest_observation_returned(app):
    c, m = app
    c.post("/api/resources/obs-paid/observe",
           json={"status": "known", "balance": 100})
    time.sleep(0.01)
    c.post("/api/resources/obs-paid/observe",
           json={"status": "known", "balance": 200})
    d = c.get("/api/resources/obs-paid/state").json()
    assert d["balance"] == 200           # 按 observed_at 取最新


def test_history_preserved(app):
    c, m = app
    c.post("/api/resources/obs-paid/observe",
           json={"status": "known", "balance": 100})
    c.post("/api/resources/obs-paid/observe",
           json={"status": "known", "balance": 200})
    assert len(m.store.observations_for("obs-paid", 50)) == 2


# ---------- 5. 删除保留 / UNREGISTERED ----------

def test_delete_preserves_observations_as_unregistered(app):
    c, m = app
    c.post("/api/resources/obs-paid/observe",
           json={"status": "known", "balance": 100})
    assert c.delete("/api/resources/obs-paid").status_code == 200
    # 历史 observation 保留
    assert len(m.store.observations_for("obs-paid", 50)) == 1
    # state 列表：unregistered
    d = c.get("/api/resources/state").json()
    unreg = [r for r in d["unregistered"] if r["resource_id"] == "obs-paid"]
    assert len(unreg) == 1 and unreg[0]["registered"] is False
    assert unreg[0]["observation_status"] == "known"
    # 单查：registered:false
    det = c.get("/api/resources/obs-paid/state").json()
    assert det["registered"] is False and det["balance"] == 100
    # 从未存在的资源 → 404
    assert c.get("/api/resources/nope/state").status_code == 404


def test_unknown_resource_observe_404(app):
    c, m = app
    assert c.post("/api/resources/nope/observe",
                  json={"status": "known"}).status_code == 404


# ---------- 6. Collector → Storage 完整闭环 ----------

def test_collector_storage_loop(app):
    c, m = app
    rd = m.resources.get("obs-paid")
    obs = ManualObservationCollector(
        {"status": "known", "balance": 50, "remaining": 450}).observe(rd)
    m.store.insert_observation(obs)
    stored = m.store.latest_observation("obs-paid")
    assert stored["status"] == "known" and stored["balance"] == 50
    assert stored["remaining"] == 450
    # API 能读到
    d = c.get("/api/resources/obs-paid/state").json()
    assert d["observation_status"] == "known" and d["balance"] == 50


# ---------- 7. analytics 不受影响 ----------

def test_analytics_unaffected_by_observations(app):
    c, m = app
    from monitor.events import AIRequestEvent
    m.store.insert(AIRequestEvent(
        provider="deepseek", model="deepseek-v4-flash", resource_id="obs-paid",
        input_tokens=5, output_tokens=1, total_tokens=6, latency_ms=100,
        status_code=200, cost=1.2e-05, timestamp=time.time()))
    c.post("/api/resources/obs-paid/observe",
           json={"status": "known", "balance": 100})
    u = c.get("/api/resources/usage").json()
    paid = [r for r in u["resources"] if r["resource_id"] == "obs-paid"][0]
    assert paid["requests"] == 1 and paid["total_tokens"] == 6
    assert paid["cost_status"] == "known"
