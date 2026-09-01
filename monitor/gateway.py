"""Monitor Gateway：统一入口，负责转发、计时、事件归一与落库。

路由形态：/gateway/{provider}/{path:path}
客户端只需把原 Provider 的 base_url 换成
    http://127.0.0.1:8787/gateway/{provider}
其余代码（SDK）零改动。
"""
from __future__ import annotations

import copy
import json
import time
from typing import Optional

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse

from .config import ConfigManager
from .core import MonitorCore
from .events import AIRequestEvent
from .providers import OpenAICompatibleAdapter
from .registry import ProviderRegistry
from .resource import ResourceRegistry
from .sanitize import sanitize_error, sanitize_json_text
from .stream import SubscriberManager
from .credential import CredentialProvider, _env_name

# 凭据边界：SECRET 仅运行时从环境变量经 CredentialProvider 解析，绝不读 config.yaml 明文
cred_provider = CredentialProvider()

router = APIRouter()

# 请求生命周期内注入（main.py 初始化）
registry: ProviderRegistry
config_mgr: ConfigManager
resources: ResourceRegistry
core: MonitorCore
http_client: httpx.AsyncClient
streams: SubscriberManager


def init(r: ProviderRegistry, c: ConfigManager, mc: MonitorCore,
         client: httpx.AsyncClient, res: Optional[ResourceRegistry] = None,
         subs: Optional[SubscriberManager] = None) -> None:
    global registry, config_mgr, resources, core, http_client, streams
    registry, config_mgr, core, http_client = r, c, mc, client
    resources = res if res is not None else ResourceRegistry()
    streams = subs if subs is not None else SubscriberManager()


def _classify_user_agent(ua: str) -> Optional[str]:
    """从 User-Agent 推导已知 Agent 身份（UA-derived client）。

    仅对明确可识别的 Agent/SDK 返回稳定 token；未知 UA 返回 None
    （绝不猜测 client）。这是 UA 推导，client_attribution_source=user_agent。
    """
    u = (ua or "").lower()
    if "codex" in u:
        return "codex"
    if "workbuddy" in u:
        return "workbuddy"
    if "openai-python" in u or "openai sdk" in u or "openai/" in u:
        return "openai-sdk"
    if "anthropic" in u or "claude" in u:
        return "anthropic-sdk"
    if u.startswith("curl/"):
        return "curl"
    if "postmanruntime" in u:
        return "postman"
    if "python-requests" in u:
        return "python-requests"
    if "python-httpx" in u or "httpx" in u:
        return "httpx"
    if "go-http-client" in u:
        return "go-client"
    if "node" in u or "axios" in u or "undici" in u:
        return "node-client"
    return None


def _resolve_project_attribution(provider: str, headers, cfg) -> tuple:
    """Project 归因（绝不猜）。

    0. X-Monitor-Project 显式头 → project_attribution_source=explicit_header
    1. provider 级 default_project（用户显式声明）→ configured_default
    2. 否则未归因（project=None）→ unattributed
    """
    explicit = headers.get("x-monitor-project")
    if explicit:
        return explicit, "explicit_header"
    if cfg is not None:
        dp = getattr(cfg, "default_project", "") or ""
        if dp:
            return dp, "configured_default"
    return None, "unattributed"


def _resolve_client_attribution(headers) -> tuple:
    """Client（发起调用的 Agent/SDK）归因（绝不猜）。

    0. X-Monitor-Client 显式头 → explicit_header
    1. 已知 User-Agent 推导（UA-derived）→ user_agent
    2. 否则未归因（client=None）→ unattributed
    注意：provider / 模型 / API Key / project 都绝不用于推导 client。
    """
    explicit = headers.get("x-monitor-client")
    if explicit:
        return explicit, "explicit_header"
    ua = headers.get("user-agent")
    if ua:
        token = _classify_user_agent(ua)
        if token:
            return token, "user_agent"
    return None, "unattributed"


def _build_event(provider: str, path: str, request: Request,
                 body: Optional[dict], adapter, cfg=None,
                 resource_id: Optional[str] = None,
                 attribution_source: str = "unattributed") -> AIRequestEvent:
    headers = request.headers
    project, project_src = _resolve_project_attribution(provider, headers, cfg)
    client, client_src = _resolve_client_attribution(headers)
    return AIRequestEvent(
        provider=provider,
        endpoint=f"/{path}",
        source=headers.get("x-monitor-source"),
        project=project,
        client=client,
        trace_id=headers.get("x-trace-id"),
        parent_id=headers.get("x-parent-id") or headers.get("x-parent-span-id"),
        resource_id=resource_id,   # 由 _resolve_resource_id 解析后传入（已校验/回退）
        model=adapter.extract_model(path, body, None),
        # 归因可解释性：保留每一笔事件"如何被归因"的来源（复用既有 metadata 字段，
        # 不改 schema、不猜、不伪造）。resource / project / client 三者分别记录来源。
        metadata={
            "attribution_source": attribution_source,
            "project_attribution_source": project_src,
            "client_attribution_source": client_src,
        },
    )


def _resolve_resource_id(provider: str, resource_header,
                         resources: ResourceRegistry,
                         config_mgr=None):
    """解析请求归属的 resource_id（经审计的最小规则，Frozen-Contract 安全）。

    可解释归因层级（每一层都有明确来源，绝不猜 Provider→Resource）：
    0. 显式 X-Monitor-Resource（非空）：权威来源，必须映射到已注册且 enabled 的
       Resource，否则拒绝（400）。attribution_source=explicit_header。
    1. provider 级 default_resource_id（用户在 config 中显式声明）：确定性映射，
       attribution_source=provider_default。无声明则跳过本层。
    2. 该 provider 恰好有【唯一】enabled Resource -> 确定性归因，
       attribution_source=unique_resource。
    3. 以上皆不满足 -> 未归因（resource_id=None），attribution_source=unattributed。
       绝不随机/猜测/按 Provider 名硬匹配。
    返回 (resource_id, error_response, attribution_source)：error_response 为 None
    表示归因成功或有意保持未归因。绝不涉及 credential 映射、绝不改写 Ledger、
    绝无新 DB 写入。
    """
    if resource_header:
        rd = resources.get(resource_header)
        if rd is None:
            return (None,
                    _error_response(provider, f"unknown resource: {resource_header}", 400),
                    "rejected_unknown")
        if not rd.enabled:
            return (None,
                    _error_response(provider, f"resource disabled: {resource_header}", 400),
                    "rejected_disabled")
        return resource_header, None, "explicit_header"
    # 无显式 header -> 确定性、可解释的归因层级（绝不猜）
    # 层级 1：provider 级显式 default_resource_id（用户声明，可解释来源）
    if config_mgr is not None:
        cfg = config_mgr.get(provider)
        dr = getattr(cfg, "default_resource_id", "") if cfg else ""
        if dr:
            rd = resources.get(dr)
            if rd is not None and rd.enabled:
                return dr, None, "provider_default"
    # 层级 2：该 provider 恰好有唯一 enabled Resource -> 确定性归因
    enabled = [r for r in resources.list(enabled_only=True)
               if r.provider == provider]
    if len(enabled) == 1:
        return enabled[0].resource_id, None, "unique_resource"
    # 层级 3：无法可靠判断 -> 未归因（绝不猜）
    return None, None, "unattributed"


def _finalize(event: AIRequestEvent, started: float, status_code: int,
              usage, error: Optional[str], cache: Optional[dict] = None) -> None:
    event.latency_ms = round((time.perf_counter() - started) * 1000, 1)
    event.status_code = status_code
    event.error = sanitize_error(error) if error else None
    event.apply_usage(usage)
    if cache:
        if cache.get("cache_read_tokens") is not None:
            event.cache_read_tokens = cache["cache_read_tokens"]
        if cache.get("cache_write_tokens") is not None:
            event.cache_write_tokens = cache["cache_write_tokens"]
        if event.cache_read_tokens is not None:
            event.cache_hit = 1 if event.cache_read_tokens > 0 else 0
    # Gateway 不计算 cost（Step 4 迁入 Core）；
    # 提交 raw event 给 Core，由 Core normalize + pricing + persist
    core.ingest(event.to_dict())


def _error_response(provider: str, message: str, status: int) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content={"error": {"message": sanitize_error(message),
                           "monitor": True, "provider": provider}},
    )


def _record_rejected(provider: str, model, message: str) -> None:
    """记录被拒绝的请求（event_type='rejected'）：token/cost 必须为 NULL，不污染 Usage 统计。"""
    try:
        evt = AIRequestEvent(provider=provider, model=model,
                             event_type="rejected", error=sanitize_error(message))
        if core is not None:
            core.ingest(evt.to_dict())
    except Exception:
        pass  # 拒绝事件落库失败不应影响主拒绝响应


def _event_snapshot(event: AIRequestEvent) -> dict:
    """发布给 SSE 的事件快照：仅公开字段，绝不包含 API Key/Prompt/Response。"""
    return {
        "request_id": event.request_id,
        "timestamp": event.timestamp,
        "provider": event.provider,
        "model": event.model,
        "source": event.source,
        "project": event.project,
        "endpoint": event.endpoint,
        "status_code": event.status_code,
        "input_tokens": event.input_tokens,
        "output_tokens": event.output_tokens,
        "total_tokens": event.total_tokens,
        "cache_read_tokens": event.cache_read_tokens,
        "cache_hit": event.cache_hit,
        "latency_ms": event.latency_ms,
        "cost": event.cost,
        "currency": event.currency,
        "error": event.error,
    }


async def _publish(event: AIRequestEvent, kind: str) -> None:
    """向所有 SSE 客户端广播。无客户端时不阻塞。"""
    if streams is None or len(streams) == 0:
        return
    await streams.broadcast({"type": kind, "event": _event_snapshot(event)})


@router.api_route("/gateway/{provider}/{path:path}",
                  methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
async def proxy(provider: str, path: str, request: Request):
    cfg = config_mgr.get(provider)
    adapter = registry.get(provider)
    if not adapter:
        # P6-F：自定义 OpenAI-compatible Provider。用户已在 config 配置 base_url + key，
        # 复用 OpenAICompatibleAdapter（同一套 usage/cost/事件链路），不另起第二套代理。
        # 仅当明确配置了 base_url 才允许，避免把任意未知路径当 Provider 路由。
        if cfg and cfg.base_url:
            adapter = OpenAICompatibleAdapter(
                name=provider,
                default_base_url=cfg.base_url,
                api_prefix="/v1",
            )
    if not adapter:
        _record_rejected(provider, None, f"unknown provider: {provider}")
        return _error_response(provider, f"unknown provider: {provider}", 404)

    if not cfg or not cfg.enabled:
        _record_rejected(provider, None, f"provider '{provider}' 未启用，请先在 Dashboard 配置")
        return _error_response(provider, f"provider '{provider}' 未启用，请先在 Dashboard 配置", 400)

    # 凭据边界：SECRET 仅运行时从环境变量经 CredentialProvider 解析，绝不读 config.yaml 明文
    key = cred_provider.get(provider)
    if not key:
        _record_rejected(provider, None,
                         f"provider '{provider}' 未配置 API Key（请在环境变量设置 "
                         f"{_env_name(provider)}）")
        return _error_response(
            provider,
            f"provider '{provider}' 未配置 API Key（请在环境变量设置 "
            f"{_env_name(provider)}）",
            400)
    # H3 修复：runtime secret 通过临时副本注入 upstream，绝不修改共享的
    # config_mgr.providers[provider] 对象，避免后续任意 save() 把真实 secret 落盘。
    # 注入的 key 仅供本次 upstream 调用使用（CredentialProvider 已校验非空）。
    runtime_cfg = copy.copy(cfg)
    runtime_cfg.api_keys = [key]

    # Resource Attribution（可解释层级：显式 header > provider default >
    # 唯一 enabled 回退 > 未归因；绝不猜、绝不回填、绝不碰凭据）
    resolved_rid, reject, attr_src = _resolve_resource_id(
        provider, request.headers.get("x-monitor-resource"), resources, config_mgr)
    if reject is not None:
        return reject

    raw_body = await request.body()
    body = None
    if raw_body:
        try:
            body = json.loads(raw_body)
        except ValueError:
            return _error_response(provider, "request body 不是合法 JSON", 400)

    event = _build_event(provider, path, request, body, adapter, cfg=cfg,
                         resource_id=resolved_rid, attribution_source=attr_src)
    url = adapter.upstream_url(runtime_cfg, path)
    if request.url.query:
        url = f"{url}?{request.url.query}"
    headers = adapter.upstream_headers(runtime_cfg, dict(request.headers))
    out_body = adapter.upstream_body(body)
    stream = adapter.is_stream(body, path)
    started = time.perf_counter()

    if stream:
        return await _proxy_stream(adapter, event, request.method, url, headers, out_body, started)
    return await _proxy_once(adapter, event, request.method, url, headers, out_body, raw_body, started)


async def _proxy_once(adapter, event, method, url, headers, body, raw_body, started):
    try:
        resp = await http_client.request(
            method, url, headers=headers,
            content=json.dumps(body).encode() if body is not None else (raw_body or None),
        )
    except httpx.HTTPError as e:
        _finalize(event, started, 502, None, f"upstream 连接失败: {type(e).__name__}")
        return _error_response(event.provider, f"upstream 连接失败: {e}", 502)

    data = adapter.safe_json(resp.text)
    usage = adapter.extract_usage(data) if isinstance(data, dict) else None
    cache = adapter.extract_cache_usage(data) if isinstance(data, dict) else None
    error = adapter.extract_error(resp.status_code, data if isinstance(data, dict) else None)
    # 响应真实 model 覆盖请求 alias（pricing 基于真实 model identity）
    if isinstance(data, dict):
        resp_model = adapter.extract_model(event.endpoint or "", None, data)
        if resp_model:
            event.model = resp_model
    _finalize(event, started, resp.status_code, usage, error, cache)
    await _publish(event, "error" if (resp.status_code >= 400 or error) else "done")

    # 错误响应透传给客户端前统一脱敏（上游错误消息可能回显 Monitor 侧 key 尾缀）
    body_bytes = resp.content
    if resp.status_code >= 400:
        body_bytes = sanitize_json_text(resp.text).encode()
    return StreamingResponse(
        iter([body_bytes]),
        status_code=resp.status_code,
        media_type=resp.headers.get("content-type", "application/json"),
    )


async def _proxy_stream(adapter, event, method, url, headers, body, started):
    collected = bytearray()
    status = 500
    # 流式请求：开始即进入 LIVE 状态
    await _publish(event, "begin")

    async def gen():
        nonlocal status
        try:
            async with http_client.stream(
                method, url, headers=headers,
                content=json.dumps(body).encode() if body is not None else None,
            ) as resp:
                status = resp.status_code
                async for chunk in resp.aiter_bytes():
                    collected.extend(chunk)
                    yield chunk
        except httpx.HTTPError as e:
            event.error = f"upstream 连接失败: {type(e).__name__}"
            status = 502  # 连接失败发生在拿到响应之前 → 与 _proxy_once 一致记为 502
        finally:
            sse_data = adapter.parse_sse_lines(bytes(collected))
            usage = adapter.extract_stream_usage(sse_data)
            cache = None
            # 流式 cache：取最后一个含 usageMetadata 的分片
            for payload in sse_data:
                chunk = adapter.safe_json(payload)
                if isinstance(chunk, dict):
                    cache = adapter.extract_cache_usage(chunk) or cache
            error = event.error
            if error is None and status >= 400:
                data = adapter.safe_json(bytes(collected).decode("utf-8", errors="replace"))
                error = adapter.extract_error(status, data if isinstance(data, dict) else None)
            _finalize(event, started, status, usage, error, cache)
            await _publish(event, "error" if (status >= 400 or error) else "done")

    return StreamingResponse(gen(), media_type="text/event-stream")
