"""AI Resource Monitor — 应用入口。

启动：
    python -m monitor.main
Dashboard:
    http://127.0.0.1:8787/
网关:
    http://127.0.0.1:8787/gateway/{provider}/...
"""
from __future__ import annotations

import asyncio
import json
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

import httpx
import uvicorn
from fastapi import FastAPI, Query
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import gateway
from .collectors.openrouter import OpenRouterObservationCollector
from .config import ConfigManager
from .core import MonitorCore
from .credential import CredentialProvider
from .observe import (ManualObservationCollector, OBS_STATUSES,
                      ObservationCollectorRegistry)
from .pricing import PricingRegistry
from .registry import ProviderRegistry
from .resource import ResourceDefinition, ResourceRegistry, \
    VALID_BILLING_MODES, VALID_RESOURCE_TYPES
from .scheduler import ObservationScheduler
from .storage import EventStore
from .stream import SubscriberManager, sse_payload

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
DASHBOARD_DIR = BASE_DIR / "dashboard"

registry = ProviderRegistry()
config_mgr = ConfigManager(DATA_DIR / "config.yaml")
pricing = PricingRegistry()
store = EventStore(DATA_DIR / "monitor.db")
core = MonitorCore(store)
resources = ResourceRegistry(config_mgr.resources)
streams = SubscriberManager()

# Provider Observation Collector Registry（无 provider 分支；新 Provider 只需 register）
observation_collectors = ObservationCollectorRegistry()
observation_collectors.register(
    "openrouter",
    OpenRouterObservationCollector(CredentialProvider()))

# 运行时 scheduler 引用（lifespan 注入；状态 API 只读）
_scheduler: Optional[ObservationScheduler] = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    client = httpx.AsyncClient(timeout=httpx.Timeout(120.0, connect=15.0))
    # 运行时重建 core/resources（测试 monkeypatch 后绑定新实例）
    core = MonitorCore(store, pricing)
    resources = ResourceRegistry(config_mgr.resources)
    gateway.init(registry, config_mgr, core, client, resources, streams)
    # Observation Scheduler（后台 asyncio 任务，不阻塞 Gateway 请求链路）
    global _scheduler
    scheduler = ObservationScheduler(resources, observation_collectors, store,
                                     config_mgr.scheduler)
    _scheduler = scheduler
    scheduler.start()
    yield
    await scheduler.stop()
    _scheduler = None
    await client.aclose()
    store.close()


app = FastAPI(title="AI Resource Monitor", lifespan=lifespan)
app.include_router(gateway.router)


# ---------------- 配置 API（绝不下发 api_key） ----------------

class ProviderIn(BaseModel):
    enabled: Optional[bool] = None
    base_url: Optional[str] = None
    api_key: Optional[str] = None
    api_keys: Optional[list[str]] = None
    test_model: Optional[str] = None


@app.get("/api/providers")
def list_providers():
    known = {p["name"] for p in config_mgr.public_view()}
    # 注册表里有但还没配置过的 provider 也列出来，方便直接启用
    missing = [
        {"name": n, "enabled": False, "base_url": registry.default_base_url(n),
         "has_key": False, "test_model": ""}
        for n in registry.names() if n not in known
    ]
    return {"providers": config_mgr.public_view() + missing}


@app.put("/api/providers/{name}")
def upsert_provider(name: str, body: ProviderIn):
    if not registry.get(name):
        return JSONResponse(status_code=404,
                            content={"error": f"unknown provider: {name}"})
    p = config_mgr.upsert(
        name,
        enabled=body.enabled,
        base_url=body.base_url,
        api_key=body.api_key if body.api_key else None,
        api_keys=body.api_keys,
        test_model=body.test_model,
    )
    return {"name": p.name, "enabled": p.enabled, "base_url": p.base_url,
            "has_key": p.has_key, "key_count": len(p.api_keys),
            "test_model": p.test_model}


# ---------------- Resource Management API ----------------
# Resource = 用户拥有的 AI 资源（一级对象）。仅操作 config_mgr.resources（单一事实来源），
# Registry 是活视图 → 保存后立即生效，无需重启。绝不保存 API Key/Secret。

_RESOURCE_ID_RE = r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$"
_SECRET_HINTS = ("sk-", "secret", "password", "bearer", "api_key",
                 "apikey", "token", "credential")


def _secret_like(text: str) -> bool:
    t = text.lower()
    return any(h in t for h in _SECRET_HINTS)


class ResourceIn(BaseModel):
    resource_id: Optional[str] = None   # 仅 POST（PUT 用路径，id 不可变）
    name: str = ""
    provider: str = ""
    resource_type: str = "other"
    billing_mode: str = "unknown"
    account_scope: str = ""
    credential_id: str = ""   # 凭据引用（如 openrouter）——真实 secret 走环境变量
    enabled: bool = True
    metadata: dict = {}


def _validate_resource(body: ResourceIn, resource_id: str) -> Optional[dict]:
    """返回错误 dict（None=通过）。不校验 provider（产品目标允许未来 Provider）。"""
    import re
    if not re.match(_RESOURCE_ID_RE, resource_id):
        return {"error": f"invalid resource_id: {resource_id!r} "
                         "(字母/数字/-/_，长度 1-64)"}
    if not body.name.strip():
        return {"error": "name is required"}
    if not body.provider.strip():
        return {"error": "provider is required"}
    if body.resource_type not in VALID_RESOURCE_TYPES:
        return {"error": f"invalid resource_type: {body.resource_type!r} "
                         f"(允许: {sorted(VALID_RESOURCE_TYPES)})"}
    if body.billing_mode not in VALID_BILLING_MODES:
        return {"error": f"invalid billing_mode: {body.billing_mode!r} "
                         f"(允许: {sorted(VALID_BILLING_MODES)})"}
    if _secret_like(body.account_scope) or any(
            c.isspace() for c in body.account_scope):
        return {"error": "account_scope 不能包含空白或疑似凭据"}
    if _secret_like(body.credential_id) or any(
            c.isspace() for c in body.credential_id):
        return {"error": "credential_id 是引用（如 openrouter），不能包含空白或疑似凭据"}
    for k, v in (body.metadata or {}).items():
        if _secret_like(str(k)) or (
                isinstance(v, str) and _secret_like(v)):
            return {"error": f"metadata 不允许疑似敏感字段: {k!r}"}
        if not isinstance(v, (str, int, float, bool, type(None))):
            return {"error": f"metadata 值必须为基本类型: {k!r}"}
    return None


def _resource_view(rd: ResourceDefinition) -> dict:
    return {
        "resource_id": rd.resource_id,
        "name": rd.name,
        "provider": rd.provider,
        "resource_type": rd.resource_type,
        "billing_mode": rd.billing_mode,
        "account_scope": rd.account_scope,
        "credential_id": rd.credential_id,
        "enabled": rd.enabled,
        "metadata": rd.metadata or {},
    }


@app.get("/api/resources")
def list_resources():
    return {"resources": [_resource_view(rd)
                          for rd in resources.list(enabled_only=False)]}


@app.post("/api/resources")
def create_resource(body: ResourceIn):
    rid = (body.resource_id or "").strip()
    if not rid:
        return JSONResponse(status_code=400, content={"error": "resource_id is required"})
    if resources.exists(rid):
        return JSONResponse(status_code=409,
                            content={"error": f"resource already exists: {rid}"})
    err = _validate_resource(body, rid)
    if err:
        return JSONResponse(status_code=422, content=err)
    rd = ResourceDefinition(
        resource_id=rid, name=body.name.strip(), provider=body.provider.strip(),
        resource_type=body.resource_type, billing_mode=body.billing_mode,
        account_scope=body.account_scope.strip(),
        credential_id=body.credential_id.strip(), enabled=body.enabled,
        metadata=body.metadata or {})
    config_mgr.upsert_resource(rd)
    return _resource_view(rd)


@app.put("/api/resources/{resource_id}")
def update_resource(resource_id: str, body: ResourceIn):
    rd = resources.get(resource_id)
    if not rd:
        return JSONResponse(status_code=404,
                            content={"error": f"unknown resource: {resource_id}"})
    err = _validate_resource(body, resource_id)
    if err:
        return JSONResponse(status_code=422, content=err)
    # resource_id 不可变（路径即稳定身份）；metadata 整体替换
    updated = ResourceDefinition(
        resource_id=resource_id, name=body.name.strip(),
        provider=body.provider.strip(), resource_type=body.resource_type,
        billing_mode=body.billing_mode,
        account_scope=body.account_scope.strip(),
        credential_id=body.credential_id.strip(), enabled=body.enabled,
        metadata=body.metadata or {})
    config_mgr.upsert_resource(updated)
    return _resource_view(updated)


@app.delete("/api/resources/{resource_id}")
def delete_resource(resource_id: str):
    if not resources.exists(resource_id):
        return JSONResponse(status_code=404,
                            content={"error": f"unknown resource: {resource_id}"})
    # 只删除定义；历史 Event.resource_id 保留（Analytics 显示为 unregistered）
    config_mgr.delete_resource(resource_id)
    return {"deleted": resource_id, "historical_events_preserved": True}


# ---------------- 统计 API ----------------

def _since(hours: Optional[float]) -> Optional[float]:
    return time.time() - hours * 3600 if hours else None


@app.get("/api/stats/overview")
def stats_overview(hours: Optional[float] = Query(None)):
    return store.overview(_since(hours))


@app.get("/api/stats/by_provider")
def stats_by_provider(hours: Optional[float] = Query(None)):
    return {"rows": store.by_provider(_since(hours)),
            "costs": store.cost_by_provider(_since(hours))}


@app.get("/api/stats/by_model")
def stats_by_model(hours: Optional[float] = Query(None)):
    return {"rows": store.by_model(_since(hours))}


@app.get("/api/stats/timeseries")
def stats_timeseries(days: int = Query(14, ge=1, le=90)):
    return {"rows": store.timeseries(days)}


@app.get("/api/events")
def list_events(limit: int = Query(100, ge=1, le=1000),
                provider: Optional[str] = None):
    return {"events": store.recent_events(limit, provider)}


# ---------------- Live Request Stream (SSE) ----------------

@app.get("/api/requests/stream")
async def request_stream():
    """SSE 实时流：type=begin|done|error。客户端断开自动清理，不影响 Gateway。"""
    async def gen():
        q = await streams.subscribe()
        try:
            yield ": connected\n\n"
            while True:
                try:
                    msg = await asyncio.wait_for(q.get(), timeout=15)
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
                    continue
                yield sse_payload(msg)
        finally:
            await streams.unsubscribe(q)

    return StreamingResponse(
        gen(), media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ---------------- 第二阶段：Analytics API ----------------
# 全部基于 SQLite 聚合，分页；时间范围 range=today|7d|30d|all


@app.get("/api/overview")
def api_overview(range: str = Query("all")):
    return store.analytics_overview(store.parse_range(range))


@app.get("/api/analytics/cost")
def api_cost(dim: str = Query("provider"),
             range: str = Query("all")):
    if dim not in ("provider", "model", "source", "project"):
        return JSONResponse(status_code=400,
                            content={"error": f"unsupported dim: {dim}"})
    return {"rows": store.analytics_cost(dim, store.parse_range(range))}


@app.get("/api/analytics/tokens")
def api_tokens(dim: str = Query("provider"),
               range: str = Query("all")):
    if dim not in ("provider", "model", "source", "project"):
        return JSONResponse(status_code=400,
                            content={"error": f"unsupported dim: {dim}"})
    return {"rows": store.analytics_tokens(dim, store.parse_range(range))}


@app.get("/api/analytics/tokens/timeseries")
def api_tokens_timeseries(days: int = Query(14, ge=1, le=90)):
    return {"rows": store.tokens_timeseries(days)}


@app.get("/api/analytics/performance")
def api_performance(range: str = Query("all")):
    return store.analytics_performance(store.parse_range(range))


@app.get("/api/analytics/performance/models")
def api_performance_models(range: str = Query("all")):
    return {"rows": store.performance_by_model(store.parse_range(range))}


@app.get("/api/requests")
def api_requests(page: int = Query(1, ge=1),
                 page_size: int = Query(20, ge=1, le=200),
                 provider: Optional[str] = None,
                 model: Optional[str] = None,
                 source: Optional[str] = None,
                 project: Optional[str] = None,
                 status: Optional[int] = None,
                 range: str = Query("all")):
    return store.query_requests(
        page=page, page_size=page_size, provider=provider, model=model,
        source=source, project=project, status=status,
        since=store.parse_range(range))


@app.get("/api/requests/{request_id}")
def api_request_detail(request_id: str):
    row = store.get_request(request_id)
    if not row:
        return JSONResponse(status_code=404,
                            content={"error": "request not found"})
    return row


@app.get("/api/sources")
def api_sources():
    """配置的 Source 列表 + 数据库中出现过的值（去重合并）。"""
    seen = store.distinct_dim_values("source")
    merged = []
    for s in config_mgr.sources + seen:
        if s not in merged:
            merged.append(s)
    return {"sources": merged, "configured": config_mgr.sources}


@app.get("/api/projects")
def api_projects():
    seen = store.distinct_dim_values("project")
    merged = []
    for p in config_mgr.projects + seen:
        if p not in merged:
            merged.append(p)
    return {"projects": merged, "configured": config_mgr.projects}


# ---------------- P0-3：Resource-aware Analytics ----------------
# 资源列表来自 ResourceRegistry（未使用的资源仍可见）；
# events.resource_id=NULL → unattributed（合法状态，不伪造资源）。


def _cost_status(requests: int, cost_count: int) -> str:
    """known: 全部有 cost；unknown: 有使用但全无 cost；mixed: 部分有；
    none: 未使用（无 cost 语义）。"""
    if requests == 0:
        return "none"
    if cost_count >= requests:
        return "known"
    if cost_count == 0:
        return "unknown"
    return "mixed"


def _resource_summary(agg: dict, resource_def=None) -> dict:
    """agg: storage.resource_usage_by_resource 单行；resource_def 可空（unattributed）。"""
    requests = agg.get("requests", 0)
    status = _cost_status(requests, agg.get("cost_count", 0))
    return {
        "resource_id": agg["resource_id"] or None,
        "resource_name": resource_def.name if resource_def else None,
        "provider": resource_def.provider if resource_def else None,
        "resource_type": resource_def.resource_type if resource_def else None,
        "billing_mode": resource_def.billing_mode if resource_def else None,
        "enabled": resource_def.enabled if resource_def else None,
        "requests": requests,
        "input_tokens": agg.get("input_tokens", 0),
        "output_tokens": agg.get("output_tokens", 0),
        "total_tokens": agg.get("total_tokens", 0),
        "cache_read_tokens": agg.get("cache_read_tokens", 0),
        "error_count": agg.get("errors", 0),
        "avg_latency_ms": round(agg.get("avg_latency_ms", 0), 1) or None,
        "last_used_at": agg.get("last_used_at"),
        "cost": (agg.get("cost_known_sum", 0)
                           if status in ("known", "mixed") else None),
        "cost_currency": None,
        "cost_status": status,
    }


@app.get("/api/resources/usage")
def api_resources_usage(range: str = Query("all")):
    """所有已配置 Resource 的 Usage Summary（含 0 使用资源）+ unattributed
    + unregistered（历史事件所属 Resource 已不在 Registry —— 不伪装成 unattributed）。"""
    since = store.parse_range(range)
    aggs = {r["resource_id"]: r for r in store.resource_usage_by_resource(since)}
    registered = resources.list(enabled_only=False)
    registered_ids = {r.resource_id for r in registered}
    out = []
    for rd in sorted(registered, key=lambda r: r.resource_id):
        agg = aggs.get(rd.resource_id, {
            "resource_id": rd.resource_id, "requests": 0, "input_tokens": 0,
            "output_tokens": 0, "total_tokens": 0, "cache_read_tokens": 0,
            "errors": 0, "avg_latency_ms": 0, "last_used_at": None,
            "cost_count": 0, "cost_known_sum": 0})
        s = _resource_summary(agg, rd)
        s["registered"] = True
        out.append(s)
    # 孤儿 resource_id：有历史事件但当前未注册（删除/从未配置）
    unregistered = []
    for rid, agg in sorted(aggs.items()):
        if rid and rid not in registered_ids:
            s = _resource_summary(agg, None)
            s["registered"] = False
            unregistered.append(s)
    unatt = aggs.get("", None)
    unattributed = (_resource_summary(unatt) if unatt else
                    {"resource_id": None, "requests": 0, "input_tokens": 0,
                     "output_tokens": 0, "total_tokens": 0, "cost": None,
                     "cost_status": "none"})
    return {"resources": out, "unregistered": unregistered,
            "unattributed": unattributed, "range": range}


@app.get("/api/resources/{resource_id}/usage")
def api_resource_usage(resource_id: str, range: str = Query("all")):
    """单 Resource：summary + model usage + recent usage。

    未注册但有历史事件的 resource_id（已删除的资源）→ 200 + registered:false；
    未注册且无任何事件 → 404（真正未知）。"""
    rd = resources.get(resource_id)
    since = store.parse_range(range)
    aggs = {r["resource_id"]: r
            for r in store.resource_usage_by_resource(since)}
    if rd is None:
        if resource_id not in aggs:
            return JSONResponse(
                status_code=404,
                content={"error": f"unknown resource: {resource_id}"})
        agg = aggs[resource_id]
        s = _resource_summary(agg, None)
        s["registered"] = False
        return {
            "resource": s,
            "models": store.model_usage_for_resource(resource_id, since),
            "recent": store.recent_for_resource(resource_id, 10),
            "registered": False,
        }
    agg = aggs.get(resource_id, {"resource_id": resource_id, "requests": 0,
                                 "input_tokens": 0, "output_tokens": 0,
                                 "total_tokens": 0, "cache_read_tokens": 0,
                                 "errors": 0, "avg_latency_ms": 0,
                                 "last_used_at": None, "cost_count": 0,
                                 "cost_known_sum": 0})
    s = _resource_summary(agg, rd)
    s["registered"] = True
    return {
        "resource": s,
        "models": store.model_usage_for_resource(resource_id, since),
        "recent": store.recent_for_resource(resource_id, 10),
        "registered": True,
    }


@app.get("/api/resources/state")
def api_resources_state():
    """所有已注册 Resource 的最新状态（无 observation 的注册资源必现，
    observation_status=no_observation）+ 未注册资源的孤儿快照（UNREGISTERED）。
    注意：声明在 /api/resources/{resource_id} 之前（FastAPI 按声明顺序匹配）。"""
    latest = {r["resource_id"]: r for r in store.all_latest_observations()}
    out = []
    for rd in sorted(resources.list(enabled_only=False),
                     key=lambda r: r.resource_id):
        s = _state_view(latest.get(rd.resource_id))
        s["resource_id"] = rd.resource_id
        s["registered"] = True
        out.append(s)
    unreg = []
    for rid, obs in sorted(latest.items()):
        if not resources.exists(rid):
            s = _state_view(obs)
            s["resource_id"] = rid
            s["registered"] = False
            unreg.append(s)
    return {"resources": out, "unregistered": unreg}


@app.get("/api/resources/{resource_id}")
def get_resource(resource_id: str):
    """单资源定义。注意：声明在 /api/resources/usage 之后（FastAPI 按声明顺序匹配，
    usage 是字面量路径，必须先于 {resource_id} 匹配）。"""
    rd = resources.get(resource_id)
    if not rd:
        return JSONResponse(status_code=404,
                            content={"error": f"unknown resource: {resource_id}"})
    return _resource_view(rd)


# ---------------- Resource Observation API ----------------
# 快照模型（与 Usage Event 分离）。四态：no_observation|known|unavailable|error。
# 原则：NULL ≠ 0；no_observation = 无任何记录；unavailable = 明确不可用；
#       error = 本应能观察但失败。绝不猜测、绝不把 cost 当 balance。


def _state_view(obs: Optional[dict]) -> dict:
    if obs is None:
        return {"observation_status": "no_observation", "balance": None,
                "quota": None, "remaining": None, "reset_at": None,
                "expires_at": None, "source": None, "error": None,
                "observed_at": None}
    return {
        "observation_status": obs["status"],
        "observed_at": obs["observed_at"],
        "balance": obs["balance"], "quota": obs["quota"],
        "remaining": obs["remaining"], "reset_at": obs["reset_at"],
        "expires_at": obs["expires_at"], "source": obs["source"],
        "error": obs["error"],
    }


@app.get("/api/resources/{resource_id}/state")
def api_resource_state(resource_id: str):
    """单资源最新状态。无 observation → 200 + no_observation（非 0 非 404）。
    未注册但有历史快照 → registered:false；未注册且无快照 → 404。"""
    rd = resources.get(resource_id)
    latest = store.latest_observation(resource_id)
    if rd is None:
        if latest is None:
            return JSONResponse(
                status_code=404,
                content={"error": f"unknown resource: {resource_id}"})
        s = _state_view(latest)
        s["resource_id"] = resource_id
        s["registered"] = False
        return s
    s = _state_view(latest)
    s["resource_id"] = resource_id
    s["registered"] = True
    return s


class ObserveIn(BaseModel):
    collector: Optional[str] = None    # 'manual' | 'auto'（默认：带 status 视为 manual，否则 auto）
    status: str = "no_observation"
    balance: Optional[float] = None
    quota: Optional[float] = None
    remaining: Optional[float] = None
    reset_at: Optional[float] = None
    expires_at: Optional[float] = None
    source: Optional[str] = None
    error: Optional[str] = None
    metadata: Optional[dict] = None


@app.post("/api/resources/{resource_id}/observe")
def api_observe_resource(resource_id: str, body: ObserveIn):
    """Observation 入口（自动路由 + 手工兼容）。

    - collector='auto'（或省略且无 status）→ Collector Registry 按 resource.provider
      自动选择；无 Collector → 存 status=unavailable（不 500）。
    - collector='manual'（或省略但带 status）→ ManualObservationCollector
      （P0-7 兼容：显式提交快照，status ∈ known|unavailable|error）。
    """
    rd = resources.get(resource_id)
    if not rd:
        return JSONResponse(status_code=404,
                            content={"error": f"unknown resource: {resource_id}"})

    # manual：显式 collector='manual'，或未指定但请求体显式携带了 status（P0-7 兼容）
    manual = body.collector == "manual" or (
        body.collector is None and "status" in body.model_fields_set)
    if manual:
        if body.status not in OBS_STATUSES or body.status == "no_observation":
            return JSONResponse(status_code=422, content={
                "error": f"invalid observation status: {body.status!r} "
                         f"(允许 known|unavailable|error)"})
        if body.status == "error" and not body.error:
            return JSONResponse(status_code=422, content={
                "error": "error 状态必须提供 error 信息"})
        try:
            obs = ManualObservationCollector(body.model_dump()).observe(rd)
        except (ValueError, TypeError) as e:
            return JSONResponse(status_code=422, content={"error": str(e)})
    else:
        # auto：按 provider 选 Collector
        collector = observation_collectors.get(rd.provider)
        if collector is None:
            obs = ManualObservationCollector(
                {"status": "unavailable", "source": "registry",
                 "error": f"no observation collector for provider {rd.provider!r}"}
            ).observe(rd)
        else:
            try:
                obs = collector.observe(rd)
            except Exception as e:   # Collector 失败 → error，不 500、不泄露
                obs = ManualObservationCollector(
                    {"status": "error", "source": "registry",
                     "error": f"collector failed: {type(e).__name__}"}).observe(rd)
    store.insert_observation(obs)
    s = _state_view(store.latest_observation(resource_id))
    s["resource_id"] = resource_id
    return s


class DimValuesIn(BaseModel):
    sources: Optional[list[str]] = None
    projects: Optional[list[str]] = None


@app.get("/api/scheduler/status")
def api_scheduler_status():
    """Observation Scheduler 状态（只读；配置在 config.yaml scheduler: 段）。"""
    if _scheduler is not None:
        return _scheduler.status()
    return {"enabled": False, "interval_seconds": None, "running": False,
            "last_cycle_at": None, "last_cycle_observations": 0}


@app.put("/api/dim-values")
def api_set_dim_values(body: DimValuesIn):
    config_mgr.set_dim_values(sources=body.sources, projects=body.projects)
    return {"sources": config_mgr.sources, "projects": config_mgr.projects}


# Dashboard 静态文件必须最后挂载（兜底路由）
app.mount("/", StaticFiles(directory=DASHBOARD_DIR, html=True), name="dashboard")


def main() -> None:
    host = config_mgr.server.get("host", "127.0.0.1")
    port = int(config_mgr.server.get("port", 8787))
    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    main()
