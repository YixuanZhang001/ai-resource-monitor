"""Generic 兜底适配器：对任意 JSON 响应的 usage 做尽力提取。

覆盖两种最常见形态：
- OpenAI-compatible：response.usage.{prompt_tokens, completion_tokens, total_tokens,
  completion_tokens_details.reasoning_tokens}
- Gemini：response.usageMetadata.{promptTokenCount, candidatesTokenCount, totalTokenCount}

无法识别为标准的 usage 字段一律进入 extension（落库前经 sanitizer 清洗），
绝不静默丢弃、绝不写入凭据。当上游协议既非 OpenAI-compat 也非 Gemini 时，
返回 None（不伪造 usage）。
"""
from __future__ import annotations

from typing import Optional

from ..config import ProviderConfig
from ..events import Usage
from .base import ProviderAdapter


class GenericAdapter(ProviderAdapter):
    name = "generic"
    usage_supported = True

    # ---------- 请求侧 ----------
    def upstream_url(self, cfg: ProviderConfig, path: str) -> str:
        base = (cfg.base_url or "").rstrip("/")
        path = path.lstrip("/")
        return f"{base}/{path}" if base else f"/{path}"

    def upstream_headers(self, cfg: ProviderConfig, client_headers: dict) -> dict:
        headers = {k: v for k, v in client_headers.items()
                   if k.lower() in {"content-type", "accept", "user-agent"}}
        headers.setdefault("content-type", "application/json")
        if cfg.has_key:
            headers["authorization"] = f"Bearer {cfg.get_active_key()}"
        return headers

    def upstream_body(self, body: Optional[dict]) -> Optional[dict]:
        if body and body.get("stream") and "stream_options" not in body:
            body = dict(body)
            body["stream_options"] = {"include_usage": True}
        return body

    # ---------- 响应侧 ----------
    def extract_model(self, path: str, body: Optional[dict],
                      response: Optional[dict]) -> Optional[str]:
        if isinstance(response, dict):
            if response.get("model"):
                return response["model"]
        if isinstance(body, dict) and body.get("model"):
            return body["model"]
        return None

    @staticmethod
    def _to_int(v) -> Optional[int]:
        return int(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None

    def extract_usage(self, response: dict) -> Optional[Usage]:
        r = response or {}
        # OpenAI-compatible
        u = r.get("usage")
        if isinstance(u, dict):
            details = u.get("completion_tokens_details") or {}
            reasoning = u.get("reasoning_tokens")
            if not isinstance(reasoning, int):
                reasoning = details.get("reasoning_tokens")
            reasoning = self._to_int(reasoning)
            known = {"prompt_tokens", "completion_tokens", "total_tokens",
                     "reasoning_tokens", "prompt_cache_hit_tokens",
                     "prompt_cache_write_tokens"}
            ext = {k: v for k, v in u.items() if k not in known}
            return Usage(
                input_tokens=self._to_int(u.get("prompt_tokens")),
                output_tokens=self._to_int(u.get("completion_tokens")),
                total_tokens=self._to_int(u.get("total_tokens")),
                reasoning_tokens=reasoning,
                cache_write_tokens=self._to_int(u.get("prompt_cache_write_tokens")),
                extension=ext or None,
            )
        # Gemini usageMetadata
        um = r.get("usageMetadata")
        if isinstance(um, dict):
            known = {"promptTokenCount", "candidatesTokenCount", "totalTokenCount",
                     "promptTokensDetails", "candidatesTokensDetails"}
            ext = {k: v for k, v in um.items() if k not in known}
            return Usage(
                input_tokens=self._to_int(um.get("promptTokenCount")),
                output_tokens=self._to_int(um.get("candidatesTokenCount")),
                total_tokens=self._to_int(um.get("totalTokenCount")),
                extension=ext or None,
            )
        return None

    def extract_stream_usage(self, sse_data: list[str]) -> Optional[Usage]:
        usage = None
        for payload in sse_data:
            chunk = self.safe_json(payload)
            if not isinstance(chunk, dict):
                continue
            if isinstance(chunk.get("usage"), dict):
                usage = chunk["usage"]
            elif isinstance(chunk.get("usageMetadata"), dict):
                return self.extract_usage(chunk)
        if isinstance(usage, dict):
            return self.extract_usage({"usage": usage})
        return None

    def extract_cache_usage(self, response: Optional[dict]) -> Optional[dict]:
        u = (response or {}).get("usage") if isinstance(response, dict) else None
        if isinstance(u, dict) and u.get("prompt_cache_hit_tokens") is not None:
            return {"cache_read_tokens": u["prompt_cache_hit_tokens"],
                    "cache_write_tokens": u.get("prompt_cache_write_tokens")}
        return None

    def extract_error(self, status_code: int, response: Optional[dict]) -> Optional[str]:
        if status_code < 400 and not (response or {}).get("error"):
            return None
        err = (response or {}).get("error")
        if isinstance(err, dict):
            return err.get("message") or str(err)
        if isinstance(err, str):
            return err
        return f"HTTP {status_code}"
