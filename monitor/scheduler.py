"""Automatic Observation Scheduler（P0-9）。

职责边界：
    Scheduler 只负责"什么时候调用谁"；Collector 负责"如何观察"；
    Storage 负责"如何保存"。Core/Gateway 完全不感知。

行为：
    - lifespan 启动 asyncio 后台任务，按 interval_seconds 周期运行
    - 遍历 enabled Resources → registry.get(provider) 找 Collector
    - 无 Collector → 跳过（不产生噪音快照；POST /observe auto 路径仍返回 unavailable）
    - collector.observe 走 asyncio.to_thread（sync httpx 不阻塞 Gateway 事件循环）
    - 单个 Resource 失败 → 写 status=error 快照，不影响其他 Resource
    - 禁用 Resource 不观察；credential 不进入任何日志/快照/API
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Callable, Optional

from .observe import ObservationCollectorRegistry, ResourceObservation
from .resource import ResourceRegistry
from .sanitize import sanitize_error
from .storage import EventStore

log = logging.getLogger(__name__)

DEFAULT_INTERVAL_SECONDS = 300


class ObservationScheduler:
    def __init__(self, resources: ResourceRegistry,
                 registry: ObservationCollectorRegistry,
                 store: EventStore,
                 config: Optional[dict] = None,
                 sleep: Optional[Callable[[float], object]] = None):
        self._resources = resources
        self._registry = registry
        self._store = store
        cfg = config or {}
        self._enabled = bool(cfg.get("enabled", True))
        self._interval = max(1, int(cfg.get("interval_seconds",
                                             DEFAULT_INTERVAL_SECONDS)))
        self._sleep = sleep or asyncio.sleep   # 测试可注入
        self._task: Optional[asyncio.Task] = None
        self.last_cycle_at: Optional[float] = None
        self.last_cycle_observations = 0

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def interval_seconds(self) -> int:
        return self._interval

    def start(self) -> None:
        if not self._enabled or self._task is not None:
            return
        self._task = asyncio.create_task(self._run(), name="observation-scheduler")
        log.info("observation scheduler started (interval=%ss)", self._interval)

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
            log.info("observation scheduler stopped")

    async def _run(self) -> None:
        while True:
            try:
                await self._cycle()
            except Exception as e:           # 周期级兜底：绝不因调度器崩溃影响 Monitor
                log.warning("observation cycle failed: %s", type(e).__name__)
            await self._sleep(self._interval)

    async def _cycle(self) -> None:
        """遍历 enabled Resources 并观察。单资源失败隔离。"""
        made = 0
        for rd in self._resources.list(enabled_only=True):
            collector = self._registry.get(rd.provider)
            if collector is None:
                continue                      # 无 Collector → 跳过（不产生噪音）
            try:
                obs = await asyncio.to_thread(collector.observe, rd)
            except Exception as e:            # Collector 异常 → error 快照（不泄露）
                obs = ResourceObservation(
                    resource_id=rd.resource_id, status="error",
                    source="scheduler",
                    error=f"collector failed: {type(e).__name__}")
            if obs.error:
                obs.error = sanitize_error(obs.error)   # 纵深防御：Collector 未脱敏则兜底
            self._store.insert_observation(obs)
            made += 1
        self.last_cycle_at = time.time()
        self.last_cycle_observations = made

    def status(self) -> dict:
        return {
            "enabled": self._enabled,
            "interval_seconds": self._interval,
            "running": self._task is not None and not self._task.done(),
            "last_cycle_at": self.last_cycle_at,
            "last_cycle_observations": self.last_cycle_observations,
        }
