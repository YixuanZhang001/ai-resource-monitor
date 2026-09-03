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
import os
import time
from datetime import datetime
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

import httpx
import uvicorn
from fastapi import FastAPI, Query
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi import File, UploadFile
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import gateway
from .collectors.deepseek import DeepSeekObservationCollector
from .collectors.openrouter import OpenRouterObservationCollector
from .config import ConfigManager
from .core import MonitorCore
from .credential import CredentialProvider, _env_name
from .credential_store import CredentialStore
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
# 数据目录可经环境变量覆盖（测试隔离用；默认仍是项目 data/）。绝不改变生产默认行为。
DATA_DIR = Path(os.environ.get("MONITOR_DATA_DIR", str(BASE_DIR / "data")))
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
observation_collectors.register(
    "deepseek",
    DeepSeekObservationCollector(CredentialProvider()))

# 运行时 scheduler 引用（lifespan 注入；状态 API 只读）
_scheduler: Optional[ObservationScheduler] = None

# 本地 manual 凭据存储（P1）。lifespan 注入具体路径；路由与 gateway 共用同一实例。
cred_store: Optional[CredentialStore] = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 显式增量列迁移：只在这里（真实启动）执行，import 期绝不 ALTER 生产库。
    store.migrate()
    client = httpx.AsyncClient(timeout=httpx.Timeout(120.0, connect=15.0))
    # 运行时重建 core/resources（测试 monkeypatch 后绑定新实例）。
    # 必须用 global：修因（P6 审计）——旧代码在此处只建了局部变量，模块级
    # 路由读到的仍是 import 期（可能为空配置）的 core/resources，导致启动后
    # 运行时配置不生效。
    global core, resources
    core = MonitorCore(store, pricing)
    resources = ResourceRegistry(config_mgr.resources)
    # P1：本地 manual 凭据存储（data/credentials.json）；仅本机、gitignore、0600。
    global cred_store
    cred_store = CredentialStore(DATA_DIR / "credentials.json")
    # gateway 与 collectors 都通过 store-backed CredentialProvider 解析凭据
    # （Environment 优先，Manual 兜底）。绝不把 secret 落 config.yaml / Ledger。
    gateway_cred = CredentialProvider(cred_store)
    gateway.cred_provider = gateway_cred
    # 让 Observation Collector 也能看到 manual 凭据（与环境变量同视角）
    observation_collectors.register(
        "openrouter", OpenRouterObservationCollector(gateway_cred))
    observation_collectors.register(
        "deepseek", DeepSeekObservationCollector(gateway_cred))
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
    default_resource_id: Optional[str] = None
    # P6-D：Provider 级默认归属 Project（用户显式声明；绝不猜）
    default_project: Optional[str] = None


@app.get("/api/providers")
def list_providers():
    # credential_source（environment/manual/None）由 store-backed provider 实时判定
    cred = CredentialProvider(cred_store) if cred_store else CredentialProvider()
    known = {p["name"] for p in config_mgr.public_view(cred)}
    # 注册表里有但还没配置过的 provider 也列出来，方便直接启用
    missing = [
        {"name": n, "enabled": False, "base_url": registry.default_base_url(n),
         "has_key": False, "credential_source": None, "test_model": ""}
        for n in registry.names() if n not in known
    ]
    return {"providers": config_mgr.public_view(cred) + missing}


@app.put("/api/providers/{name}")
def upsert_provider(name: str, body: ProviderIn):
    # 已知 Provider 直接允许；自定义 OpenAI-compatible Provider 需明确提供
    # base_url + enabled=True（Gateway 才会用 OpenAICompatibleAdapter 路由，不另起代理）。
    if not registry.get(name) and not (body.base_url and body.enabled):
        return JSONResponse(status_code=404, content={
            "error": f"unknown provider: {name} "
                     "(custom OpenAI-compatible provider requires base_url + enabled=true)"})
    p = config_mgr.upsert(
        name,
        enabled=body.enabled,
        base_url=body.base_url,
        api_key=body.api_key if body.api_key else None,
        api_keys=body.api_keys,
        test_model=body.test_model,
        default_resource_id=body.default_resource_id,
        default_project=body.default_project,
    )
    # has_key / credential_source 由 CredentialProvider 实时判定，
    # 不反映任何已落盘的 secret（Phase 1E-C 安全边界；P1 含 manual 来源）。
    cred = CredentialProvider(cred_store) if cred_store else CredentialProvider()
    has_key = cred.available(name)
    src = cred.source(name)
    return {"name": p.name, "enabled": p.enabled, "base_url": p.base_url,
            "has_key": has_key, "key_count": 1 if has_key else 0,
            "credential_source": src, "test_model": p.test_model,
            "default_resource_id": p.default_resource_id,
            "default_project": p.default_project}


# ---------------- Credential Access API (P1) ----------------
# 两条本地凭据来源：
#   A. Environment Variable（既有，优先级最高）
#   B. Manual（本地 data/credentials.json，本端点写入）
# 安全铁律：响应绝不返回 key 本体 / 密文 / 存储内部；仅返回安全元数据。
# 真实 secret 仅运行时经 CredentialProvider 解析给 Gateway 使用。


class CredentialIn(BaseModel):
    api_key: str                       # 明文仅在本请求体内，绝不存储/回显/落 config


@app.put("/api/credentials/{provider}")
def put_credential(provider: str, body: CredentialIn):
    # 已知 Provider 或已配置的自定义 Provider（config_mgr 中存在）均可保存凭据；
    # 凭据边界不变：仅存本机 store，绝不回显 / 落 config / 进前端。
    if not registry.get(provider) and not config_mgr.get(provider):
        return JSONResponse(status_code=404,
                            content={"error": f"unknown provider: {provider}"})
    if cred_store is None:
        return JSONResponse(status_code=500,
                            content={"error": "credential store unavailable"})
    try:
        cred_store.save(provider, body.api_key)
    except ValueError as e:
        return JSONResponse(status_code=422, content={"error": str(e)})
    # 绝不回显 key；仅返回安全状态。Environment 仍优先于本 manual 值。
    return {"provider": provider, "credential_available": True,
            "credential_source": "manual"}


@app.delete("/api/credentials/{provider}")
def delete_credential(provider: str):
    if cred_store is None:
        return JSONResponse(status_code=500,
                            content={"error": "credential store unavailable"})
    removed = cred_store.delete(provider)
    return {"provider": provider, "removed": removed,
            "credential_source": "environment" if os.environ.get(
                _env_name(provider)) else None}


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
    since = store.parse_range(range)
    ov = store.analytics_overview(since)
    pricing = None
    try:
        if config_mgr is not None:
            pricing = {}
            for name, pc in config_mgr.providers.items():
                cp = (getattr(pc, "extra", None) or {}).get("cache_pricing")
                if cp:
                    pricing[name] = cp
    except Exception:
        pricing = None
    ov["cache_savings_estimate"] = store.cache_savings(since, pricing)
    return ov


@app.get("/api/analytics/cost")
def api_cost(dim: str = Query("provider"),
             range: str = Query("all")):
    if dim not in ("provider", "model", "source", "project", "client"):
        return JSONResponse(status_code=400,
                            content={"error": f"unsupported dim: {dim}"})
    return {"rows": store.analytics_cost(dim, store.parse_range(range))}


@app.get("/api/analytics/tokens")
def api_tokens(dim: str = Query("provider"),
               range: str = Query("all")):
    if dim not in ("provider", "model", "source", "project", "client"):
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


# ---------------- Phase 3B：Efficiency Derivation Layer ----------------
# 覆盖度感知的效率派生层（纯 SQL 聚合 + 比率在 Python 计算）。
# 既有 /api/analytics/* 保持不变（向后兼容）；效率层为新增能力，统一挂在
# /api/efficiency/* 下，避免改动既有响应结构。禁止：新 Provider / quota /
# health / rate-limit / subscription / billing_status / 新表 / schema 改造。

_EFFICIENCY_DIMS = ("provider", "model", "source", "project",
                    "client", "resource_id")


@app.get("/api/efficiency/overview")
def api_efficiency_overview(range: str = Query("all")):
    """全局效率概览：覆盖度 + 各比率（多货币分别计价）。"""
    return store.efficiency_overview(store.parse_range(range))


@app.get("/api/efficiency/by_dim")
def api_efficiency_by_dim(dim: str = Query("provider"),
                          range: str = Query("all")):
    """按维度（provider/model/source/project/resource_id）聚合效率，
    每条带 requests/errors/error_rate/tokens(覆盖度)/cost(覆盖度)/cache/latency。"""
    if dim not in _EFFICIENCY_DIMS:
        return JSONResponse(
            status_code=400,
            content={"error": f"unsupported dim: {dim} "
                             f"(允许: {_EFFICIENCY_DIMS})"})
    return {"dim": dim,
            "rows": store.efficiency_by_dim(dim, store.parse_range(range))}


@app.get("/api/resources/{resource_id}/balance-trend")
def api_balance_trend(resource_id: str,
                      limit: int = Query(20, ge=1, le=200)):
    """余额观测趋势。返回每期 balance 与相邻已知余额差（balance_delta）。

    注意：绝不返回 burn_rate / API 消费速度字段；observed_balance_change
    仅为观测余额差值，不代表 API 实际消耗。"""
    return store.balance_trend(resource_id, limit)


@app.get("/api/resources/{resource_id}/health")
def api_resource_health(resource_id: str,
                        since: Optional[float] = Query(None)):
    """Resource 级最小 Health 派生（基于真实请求 status/error，非探测）。

    无数据 -> unknown（不谎报 healthy）；n=0 时 error_rate=None。"""
    return store.resource_health(resource_id, since)


@app.get("/api/requests")
def api_requests(page: int = Query(1, ge=1),
                 page_size: int = Query(20, ge=1, le=200),
                 provider: Optional[str] = None,
                 model: Optional[str] = None,
                 source: Optional[str] = None,
                 project: Optional[str] = None,
                 client: Optional[str] = None,
                 status: Optional[int] = None,
                 range: str = Query("all")):
    # client 维度（P6-E）：storage 早支持，此前 /api/requests 漏接该参数，
    # 导致 Dashboard 的 Client 筛选器被静默丢弃。
    return store.query_requests(
        page=page, page_size=page_size, provider=provider, model=model,
        source=source, project=project, client=client, status=status,
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


@app.get("/api/clients")
def api_clients():
    """已观测到的 Client（Agent/SDK/工具）列表：从 events.client 去重聚合。"""
    seen = store.distinct_dim_values("client")
    return {"clients": seen}


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
        # 修因（P6 审计）：多币种时绝不给单个汇总数字（无汇率、绝不换算），
        # 交由 cost_by_currency 分组展示；旧实现把 CNY+USD 相加并硬编码币种。
        "cost": (agg.get("cost_known_sum", 0)
                 if status in ("known", "mixed")
                 and not agg.get("mixed_currency") else None),
        "cost_currency": agg.get("cost_currency"),
        "cost_by_currency": agg.get("cost_by_currency") or {},
        "mixed_currency": bool(agg.get("mixed_currency")),
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
            "cost_count": 0, "cost_known_sum": 0,
            "cost_by_currency": {}, "cost_currency": None,
            "mixed_currency": False})
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
                     "cost_currency": None, "cost_by_currency": {},
                     "mixed_currency": False, "cost_status": "none"})
    return {"resources": out, "unregistered": unregistered,
            "unattributed": unattributed, "range": range}


@app.get("/api/attribution/coverage")
def api_attribution_coverage(range: str = Query("all")):
    """透明归因覆盖度（read-only 聚合，无新数据层）。

    回答「为什么我的 Resource 数据这么少」：
    - registered_requests    : resource_id 命中当前 Resource Registry 的请求
    - unregistered_requests  : resource_id 非 NULL 但已不在 Registry（删除/从未注册）
    - unattributed_requests  : resource_id IS NULL（未打标，绝不猜测归属）
    - coverage_pct = registered_requests / total_requests（total=0 时为 None）
    三桶严格分离；不自动把 unknown 请求归给 Resource、不改历史语义、不引 AI 归因。
    """
    since = store.parse_range(range)
    aggs = {r["resource_id"]: r for r in store.resource_usage_by_resource(since)}
    registered_ids = {r.resource_id for r in resources.list(enabled_only=False)}
    total = registered = unregistered = unattributed = 0
    unreg_ids: set[str] = set()
    for rid, agg in aggs.items():
        req = agg.get("requests", 0) or 0
        total += req
        if rid == "":
            unattributed += req
        elif rid in registered_ids:
            registered += req
        else:
            unregistered += req
            unreg_ids.add(rid)
    coverage = (registered / total) if total else None
    return {
        "range": range,
        "total_requests": total,
        "registered_requests": registered,
        "unregistered_requests": unregistered,
        "unattributed_requests": unattributed,
        "coverage_pct": round(coverage * 100, 2) if coverage is not None else None,
        "registered_resource_count": len(registered_ids),
        "unregistered_resource_count": len(unreg_ids),
        "note": ("coverage_pct = registered_requests / total_requests; "
                 "unattributed (resource_id IS NULL) 与 unregistered (已不在 Registry) "
                 "均不计入 registered，绝不猜测归属"),
    }


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


# ---------------- Phase 4 Step 1：Resource-level Trend ----------------
# 纯 events 每日时间序列聚合；不新增表/schema；NULL 保留为独立未归因桶
# （显式 /unattributed/timeseries 端点，绝不混入任何 Resource）。

@app.get("/api/resources/unattributed/timeseries")
def api_unattributed_timeseries(days: int = Query(30, ge=1, le=365)):
    """未归因流量每日趋势（resource_id IS NULL）。

    与 /api/resources/usage 的 unattributed 语义一致：NULL 保留为独立桶，
    绝不混入任何 Resource，也不猜测归属（不回填历史、不伪造）。"""
    return {"resource_id": None,
            "rows": store.resource_timeseries(None, days)}


@app.get("/api/resources/{resource_id}/timeseries")
def api_resource_timeseries(resource_id: str,
                            days: int = Query(30, ge=1, le=365)):
    """单 Resource 每日趋势：usage/cost/tokens/errors/latency。

    纯 events 聚合（与资源注册定义无关）；无该 resource_id 事件 → 200 + 空 rows。
    resource_id IS NULL 的事件绝不混入（由 /unattributed/timeseries 显式暴露）。"""
    return {"resource_id": resource_id,
            "rows": store.resource_timeseries(resource_id, days)}


def _obs_stale_interval() -> int:
    """复用 scheduler 配置 interval（项目已有，不硬编码）。

    _scheduler 在 lifespan 注入；未启动（如测试）则回退 config_mgr.scheduler。
    stale 阈值 = 2 × interval（observation 超过两周期未刷新视为过期）。"""
    iv = _scheduler.interval_seconds if _scheduler is not None else None
    if not iv:
        iv = config_mgr.scheduler.get("interval_seconds", 300)
    return max(1, int(iv))


def _enrich_state(s: dict, resource_id: str, interval: int) -> None:
    """分离 Resource Health 与 Observation Status（本轮核心修复）。

    - health：基于真实请求 events 派生（authoritative），回答"Resource 本身是否健康"
    - stale：最新 observation 距今 > 2×interval 视为过期（Monitor 可能已停止），
      避免冻结的 observation error 永久伪装成 Resource ERROR
    observation_status 仍保留，语义为"Monitor 能否完成观察"，不混入 health。
    """
    s["health"] = store.resource_health(resource_id)
    observed_at = s.get("observed_at")
    s["stale"] = bool(observed_at) and (
        time.time() - float(observed_at) > 2 * interval)


@app.get("/api/resources/state")
def api_resources_state():
    """所有已注册 Resource 的最新状态（无 observation 的注册资源必现，
    observation_status=no_observation）+ 未注册资源的孤儿快照（UNREGISTERED）。
    每个 resource 附加 authoritative Resource Health + stale 标记（核心修复：
    observation failure 不再被误判为 Resource ERROR）。
    注意：声明在 /api/resources/{resource_id} 之前（FastAPI 按声明顺序匹配）。"""
    latest = {r["resource_id"]: r for r in store.all_latest_observations()}
    interval = _obs_stale_interval()
    out = []
    for rd in sorted(resources.list(enabled_only=False),
                     key=lambda r: r.resource_id):
        s = _state_view(latest.get(rd.resource_id))
        _enrich_state(s, rd.resource_id, interval)
        s["resource_id"] = rd.resource_id
        s["registered"] = True
        out.append(s)
    unreg = []
    for rid, obs in sorted(latest.items()):
        if not resources.exists(rid):
            s = _state_view(obs)
            _enrich_state(s, rid, interval)
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


def _obs_currency(obs: Optional[dict]) -> Optional[str]:
    """快照金额（balance）的币种，来自 collector 写入的 metadata.currency。
    取不到就返回 None —— 绝不按 provider 猜测币种（跨币种不可求和）。"""
    if not obs:
        return None
    raw = obs.get("metadata")
    if not raw:
        return None
    try:
        meta = json.loads(raw) if isinstance(raw, str) else raw
    except (ValueError, TypeError):
        return None
    if not isinstance(meta, dict):
        return None
    cur = meta.get("currency")
    return cur if isinstance(cur, str) and cur else None


def _state_view(obs: Optional[dict]) -> dict:
    if obs is None:
        return {"observation_status": "no_observation", "balance": None,
                "quota": None, "remaining": None, "reset_at": None,
                "expires_at": None, "source": None, "error": None,
                "observed_at": None, "currency": None}
    return {
        "observation_status": obs["status"],
        "observed_at": obs["observed_at"],
        "balance": obs["balance"], "quota": obs["quota"],
        "remaining": obs["remaining"], "reset_at": obs["reset_at"],
        "expires_at": obs["expires_at"], "source": obs["source"],
        "error": obs["error"],
        "currency": _obs_currency(obs),
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
        _enrich_state(s, resource_id, _obs_stale_interval())
        s["resource_id"] = resource_id
        s["registered"] = False
        return s
    s = _state_view(latest)
    _enrich_state(s, resource_id, _obs_stale_interval())
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


class BillingImportIn(BaseModel):
    amount_path: str
    cost_path: str
    provider: str = "deepseek"
    prune_gateway: bool = True


def _backup_db(db_path: Path) -> Optional[str]:
    """导入前对生产库做时间点备份（复制），返回备份路径；失败返回 None。"""
    try:
        import shutil
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        bak = db_path.with_suffix(f".db.bak-import-{ts}")
        shutil.copy2(db_path, bak)
        return str(bak)
    except Exception:
        return None


@app.post("/api/import/billing")
def api_import_billing(body: BillingImportIn):
    """导入平台账单 CSV（amount/cost）为 llm_call 事件。生产库变更前自动备份。"""
    amount = Path(body.amount_path)
    cost = Path(body.cost_path)
    if not amount.is_file() or not cost.is_file():
        return JSONResponse(status_code=400,
                            content={"error": "amount/cost 路径不存在"})
    if amount.suffix.lower() != ".csv" or cost.suffix.lower() != ".csv":
        return JSONResponse(status_code=400, content={"error": "仅支持 .csv"})
    db_path = DATA_DIR / "monitor.db"
    backup = _backup_db(db_path)
    try:
        from .billing_import import import_bills
        res = import_bills(str(amount), str(cost), str(db_path),
                          provider=body.provider, prune_gateway=body.prune_gateway)
    except Exception as e:  # noqa: BLE001
        return JSONResponse(status_code=500,
                            content={"error": f"导入失败: {e}", "backup": backup})
    return {"result": res, "backup": backup}


@app.post("/api/import/billing-upload")
async def api_import_billing_upload(
    amount: UploadFile = File(...),
    cost: UploadFile = File(...),
    provider: str = "deepseek",
    prune_gateway: bool = True,
):
    """浏览器上传两份 CSV 后导入。文件落到 DATA_DIR/.import_upload/ 再走同一条链路。"""
    if (amount.filename or "").lower().endswith(".csv") is False \
            or (cost.filename or "").lower().endswith(".csv") is False:
        return JSONResponse(status_code=400, content={"error": "仅支持 .csv"})
    updir = DATA_DIR / ".import_upload"
    updir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    amount_path = updir / f"{ts}-amount.csv"
    cost_path = updir / f"{ts}-cost.csv"
    try:
        amount_path.write_bytes(await amount.read())
        cost_path.write_bytes(await cost.read())
    except Exception as e:  # noqa: BLE001
        return JSONResponse(status_code=500, content={"error": f"写入失败: {e}"})
    db_path = DATA_DIR / "monitor.db"
    backup = _backup_db(db_path)
    try:
        from .billing_import import import_bills
        res = import_bills(str(amount_path), str(cost_path), str(db_path),
                          provider=provider, prune_gateway=prune_gateway)
    except Exception as e:  # noqa: BLE001
        return JSONResponse(status_code=500,
                            content={"error": f"导入失败: {e}", "backup": backup})
    return {"result": res, "backup": backup}


# Dashboard 静态文件必须最后挂载（兜底路由）
app.mount("/", StaticFiles(directory=DASHBOARD_DIR, html=True), name="dashboard")


def main() -> None:
    host = config_mgr.server.get("host", "127.0.0.1")
    port = int(config_mgr.server.get("port", 8787))
    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    import sys
    import argparse

    argv = sys.argv[1:]
    if argv and argv[0] == "import-bills":
        parser = argparse.ArgumentParser(prog="monitor.main import-bills")
        parser.add_argument("--amount", required=True, help="amount-*.csv 路径")
        parser.add_argument("--cost", required=True, help="cost-*.csv 路径")
        parser.add_argument("--db", default=None, help="可选：DB 路径（默认 data/monitor.db）")
        parser.add_argument("--provider", default="deepseek")
        parser.add_argument("--no-prune", action="store_true",
                            help="不删除同区间网关采集事件（允许重叠计数）")
        args = parser.parse_args(argv[1:])
        from .billing_import import import_bills
        db_path = args.db or str(DATA_DIR / "monitor.db")
        res = import_bills(args.amount, args.cost, db_path,
                          provider=args.provider, prune_gateway=not args.no_prune)
        print("import result:", res)
    else:
        main()
