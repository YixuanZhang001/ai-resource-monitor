"""OpenAI-compatible 通用适配器。

OpenAI / DeepSeek / Kimi / MiniMax / OpenRouter 等遵循
`POST {base_url}/v1/chat/completions` 协议的 Provider 全部复用本实现，
差异（base_url、鉴权头、usage 字段细节）通过构造参数注入。
"""
from __future__ import annotations

from typing import Optional

from ..config import ProviderConfig
from ..events import Usage
from .base import ProviderAdapter

# 透传给上游时允许保留的头（白名单制，避免泄漏客户端鉴权/Cookie）
PASSTHROUGH_HEADERS = {"content-type", "accept", "user-agent"}


class OpenAICompatibleAdapter(ProviderAdapter):
    def __init__(self, name: str, default_base_url: str, api_prefix: str = "/v1",
                 auth_header: str = "authorization",
                 auth_scheme: str = "Bearer "):
        self.name = name
        self.default_base_url = default_base_url
        self.api_prefix = api_prefix.rstrip("/")
        self.auth_header = auth_header
        self.auth_scheme = auth_scheme

    # ---------- 请求侧 ----------

    def _base(self, cfg: ProviderConfig) -> str:
        return (cfg.base_url or self.default_base_url).rstrip("/")

    def upstream_url(self, cfg: ProviderConfig, path: str) -> str:
        path = path.lstrip("/")
        # 客户端如果已经带了 v1 前缀（直接把 base_url 指到 gateway）则不再补
        if self.api_prefix and not path.startswith(self.api_prefix.lstrip("/") + "/"):
            path = f"{self.api_prefix.lstrip('/')}/{path}"
        return f"{self._base(cfg)}/{path}"

    def upstream_headers(self, cfg: ProviderConfig, client_headers: dict) -> dict:
        headers = {
            k: v for k, v in client_headers.items()
            if k.lower() in PASSTHROUGH_HEADERS
        }
        headers.setdefault("content-type", "application/json")
        if cfg.has_key:
            headers[self.auth_header] = f"{self.auth_scheme}{cfg.get_active_key()}"
        return headers

    def upstream_body(self, body: Optional[dict]) -> Optional[dict]:
        # 流式请求注入 stream_options，让上游在末尾返回 usage（OpenAI/DeepSeek/Kimi/MiniMax 均支持）
        if body and body.get("stream") and "stream_options" not in body:
            body = dict(body)
            body["stream_options"] = {"include_usage": True}
        return body

    # ---------- 响应侧 ----------

    def extract_model(self, path: str, body: Optional[dict],
                      response: Optional[dict]) -> Optional[str]:
        """Model identity：响应回显优先（真实 model），回退请求 body（alias）。

        DeepSeek 等会把请求 deepseek-chat 别名到 deepseek-v4-flash 回显，
        pricing 必须基于响应真实 model，否则命中错误价格档。
        """
        if response and response.get("model"):
            return response["model"]
        if body and body.get("model"):
            return body["model"]
        return None

    def extract_usage(self, response: dict) -> Optional[Usage]:
        u = (response or {}).get("usage")
        if not u:
            return None
        return Usage(
            input_tokens=u.get("prompt_tokens"),
            output_tokens=u.get("completion_tokens"),
            total_tokens=u.get("total_tokens"),
        )

    def extract_cache_usage(self, response: Optional[dict]) -> Optional[dict]:
        """OpenAI-compatible 上游 cache 提取。

        DeepSeek V4 真实字段（2026-08-20 真实验证确认）：
          usage.prompt_cache_hit_tokens   命中缓存的 prompt token（→ cache_read）
          usage.prompt_cache_miss_tokens   未命中（不映射 cache_write；属 input）
          usage.cached_tokens              别名/总数（当前不用）
        其余 OpenAI-compat Provider 未返回这些字段时返回 None（不伪造）。
        """
        u = ((response or {}).get("usage")) or {}
        hit = u.get("prompt_cache_hit_tokens")
        if hit is None:
            return None
        return {"cache_read_tokens": hit, "cache_write_tokens": None}

    def extract_stream_usage(self, sse_data: list[str]) -> Optional[Usage]:
        usage = None
        for payload in sse_data:
            chunk = self.safe_json(payload)
            if isinstance(chunk, dict) and chunk.get("usage"):
                usage = chunk["usage"]   # 最后一个非空 usage 为准
        if not usage:
            return None
        return Usage(
            input_tokens=usage.get("prompt_tokens"),
            output_tokens=usage.get("completion_tokens"),
            total_tokens=usage.get("total_tokens"),
        )

    def extract_error(self, status_code: int, response: Optional[dict]) -> Optional[str]:
        if status_code < 400 and not (response or {}).get("error"):
            return None
        err = (response or {}).get("error")
        if isinstance(err, dict):
            return err.get("message") or str(err)
        if isinstance(err, str):
            return err
        return f"HTTP {status_code}"
