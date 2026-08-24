"""P0-9 测试：Automatic Observation Scheduler。

覆盖：config 加载、disabled 跳过、无 collector 跳过（零快照）、mock collector
调用、单资源失败隔离、两周期历史保留、start/stop 生命周期、credential 不泄露、
间隔注入（不真实等待）。
"""
import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from monitor.observe import (  # noqa: E402
    ObservationCollectorRegistry, ObservationCollector, ResourceObservation)
from monitor.resource import ResourceDefinition, ResourceRegistry  # noqa: E402
from monitor.scheduler import ObservationScheduler  # noqa: E402
from monitor.storage import EventStore  # noqa: E402


class FakeCollector(ObservationCollector):
    def __init__(self, result="known", calls=None, secret="sk-fake"):
        self.result = result
        self.calls = calls if calls is not None else []
        self.secret = secret

    def observe(self, resource) -> ResourceObservation:
        self.calls.append(resource.resource_id)
        if self.result == "raise":
            raise RuntimeError("boom")
        if self.result == "error":
            return ResourceObservation(
                resource_id=resource.resource_id, status="error",
                source="api", error=f"upstream said {self.secret}123")
        return ResourceObservation(
            resource_id=resource.resource_id, status="known",
            balance=100.0, remaining=80.0, source="api")


class BoomCollector(ObservationCollector):
    def observe(self, resource) -> ResourceObservation:
        raise RuntimeError("collector exploded")


def _rd(resource_id, provider="openrouter", enabled=True):
    return ResourceDefinition(resource_id=resource_id, provider=provider,
                              enabled=enabled)


def _env(resources, registry, store, **cfg):
    return ObservationScheduler(
        ResourceRegistry({r.resource_id: r for r in resources}),
        registry, store, {"enabled": True, "interval_seconds": 300, **cfg})


def test_config_load_defaults(tmp_path):
    from monitor.config import ConfigManager
    p = tmp_path / "c.yaml"
    p.write_text("server: {host: '127.0.0.1', port: 8787}\n", encoding="utf-8")
    cm = ConfigManager(p)
    assert cm.scheduler["enabled"] is True
    assert cm.scheduler["interval_seconds"] == 300
    p2 = tmp_path / "c2.yaml"
    p2.write_text("scheduler: {enabled: false, interval_seconds: 60}\n",
                  encoding="utf-8")
    cm2 = ConfigManager(p2)
    assert cm2.scheduler["enabled"] is False
    assert cm2.scheduler["interval_seconds"] == 60


def test_cycle_skips_disabled_resources(tmp_path):
    store = EventStore(tmp_path / "t.db")
    reg = ObservationCollectorRegistry()
    fake = FakeCollector()
    reg.register("openrouter", fake)
    s = _env([_rd("off-res", enabled=False), _rd("on-res")], reg, store)
    asyncio.run(s._cycle())
    assert fake.calls == ["on-res"]                 # 禁用资源未观察
    assert len(store.observations_for("off-res", 10)) == 0


def test_cycle_skips_no_collector(tmp_path):
    store = EventStore(tmp_path / "t.db")
    reg = ObservationCollectorRegistry()            # 空 registry
    s = _env([_rd("deepseek-res", provider="deepseek")], reg, store)
    asyncio.run(s._cycle())
    # 无 collector → 跳过，不产生噪音快照
    assert len(store.observations_for("deepseek-res", 10)) == 0


def test_cycle_calls_collector_and_stores(tmp_path):
    store = EventStore(tmp_path / "t.db")
    reg = ObservationCollectorRegistry()
    fake = FakeCollector()
    reg.register("openrouter", fake)
    s = _env([_rd("or-a")], reg, store)
    asyncio.run(s._cycle())
    assert fake.calls == ["or-a"]
    snap = store.latest_observation("or-a")
    assert snap["status"] == "known" and snap["balance"] == 100.0
    assert snap["remaining"] == 80.0 and snap["source"] == "api"
    assert s.last_cycle_observations == 1


def test_single_resource_failure_isolated(tmp_path):
    """一个 Collector 抛异常 → error 快照；另一个正常 → known。互不影响。"""
    store = EventStore(tmp_path / "t.db")
    reg = ObservationCollectorRegistry()
    reg.register("openrouter", BoomCollector())
    reg.register("ollama", FakeCollector())          # 另一个 provider
    s = _env([_rd("bad-res"), _rd("good-res", provider="ollama")], reg, store)
    asyncio.run(s._cycle())
    bad = store.latest_observation("bad-res")
    good = store.latest_observation("good-res")
    # 只记录异常类型名（不存任意异常正文，防泄露），且不影响其他资源
    assert bad["status"] == "error" and "collector failed: RuntimeError" in bad["error"]
    assert "collector exploded" not in bad["error"]
    assert good["status"] == "known" and good["balance"] == 100.0


def test_two_cycles_preserve_history(tmp_path):
    store = EventStore(tmp_path / "t.db")
    reg = ObservationCollectorRegistry()
    reg.register("openrouter", FakeCollector())
    s = _env([_rd("or-a")], reg, store)
    asyncio.run(s._cycle())
    asyncio.run(s._cycle())
    assert len(store.observations_for("or-a", 50)) == 2   # 两个时间点


def test_start_stop_lifecycle(tmp_path):
    store = EventStore(tmp_path / "t.db")
    reg = ObservationCollectorRegistry()
    reg.register("openrouter", FakeCollector())
    slept = []
    async def fake_sleep(sec):
        slept.append(sec)

    s = ObservationScheduler(ResourceRegistry({"or-a": _rd("or-a")}), reg,
                             store,
                             {"enabled": True, "interval_seconds": 300},
                             sleep=fake_sleep)

    async def _run():
        s.start()
        assert s.status()["running"] is True
        await asyncio.sleep(0.05)                # 让第一个 cycle 跑完
        await s.stop()
        assert s.status()["running"] is False
        assert s.status()["last_cycle_at"] is not None

    asyncio.run(_run())
    assert store.latest_observation("or-a")["status"] == "known"
    assert len(slept) >= 1                        # 周期间隔已发生


def test_disabled_scheduler_does_not_start(tmp_path):
    store = EventStore(tmp_path / "t.db")
    reg = ObservationCollectorRegistry()
    s = ObservationScheduler(ResourceRegistry({}), reg, store,
                             {"enabled": False, "interval_seconds": 300})
    s.start()
    assert s.status()["running"] is False


def test_credential_not_leaked_in_error_snapshot(tmp_path):
    store = EventStore(tmp_path / "t.db")
    reg = ObservationCollectorRegistry()
    reg.register("openrouter", FakeCollector(result="error", secret="sk-fake-xyz"))
    s = _env([_rd("or-a")], reg, store)
    asyncio.run(s._cycle())
    snap = store.latest_observation("or-a")
    assert snap["status"] == "error"
    assert "sk-fake-xyz" not in snap["error"]       # 真实 secret 已清除
    assert "REDACTED" in snap["error"].upper()      # sanitizer 脱敏标记（非泄露）


def test_cycle_runs_off_event_loop_without_blocking(tmp_path):
    """observe 在 to_thread 执行（异步调度不阻塞事件循环）。"""
    import threading
    store = EventStore(tmp_path / "t.db")
    reg = ObservationCollectorRegistry()
    tid = {}

    class ThreadAware(ObservationCollector):
        def observe(self, resource):
            tid["thread"] = threading.current_thread().name
            return ResourceObservation(resource_id=resource.resource_id,
                                       status="known", balance=1.0, source="api")

    reg.register("openrouter", ThreadAware())
    s = _env([_rd("or-a")], reg, store)
    asyncio.run(s._cycle())
    # observe 在 worker 线程（非主事件循环线程）执行
    assert tid.get("thread") not in (None, "MainThread")
