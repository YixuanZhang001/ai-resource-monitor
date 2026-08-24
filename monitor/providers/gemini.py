"""Gemini 原生适配器（generateContent / streamGenerateContent 协议）。

协议差异（鉴权方式、模型在 URL 路径中、usageMetadata 结构）
全部在本 Adapter 内解决，Monitor Core 无感知。
"""
from __future__ import annotations

import re
from typing import Optional

from ..config import ProviderConfig
from ..events import Usage
from .base import ProviderAdapter

MODEL_RE = re.compile(r"models/([^/:]+)")


class GeminiAdapter(ProviderAdapter):
    def __init__(self):
        self.name = "gemini"
        self.default_base_url = "https://generativelanguage.googleapis.com"
        self.balance_supported = False
        self.usage_supported = False
        self.cache_supported = True   # usageMetadata.cachedContentTokenCount 真实字段

    # ---------- 请求侧 ----------

    def upstream_url(self, cfg: ProviderConfig, path: str) -> str:
        base = (cfg.base_url or self.default_base_url).rstrip("/")
        return f"{base}/{path.lstrip('/')}"

    def upstream_headers(self, cfg: ProviderConfig, client_headers: dict) -> dict:
        headers = {"content-type": "application/json"}
        if cfg.has_key:
            headers["x-goog-api-key"] = cfg.get_active_key()
        return headers

    @staticmethod
    def is_stream(body: Optional[dict], path: str) -> bool:
        return ":streamGenerateContent" in path

    # ---------- 响应侧 ----------

    def extract_model(self, path: str, body: Optional[dict],
                      response: Optional[dict]) -> Optional[str]:
        m = MODEL_RE.search(path)
        if m:
            return m.group(1)
        if response and response.get("modelVersion"):
            return response["modelVersion"]
        return None

    @staticmethod
    def _usage_from_metadata(meta: dict) -> Usage:
        return Usage(
            input_tokens=meta.get("promptTokenCount"),
            output_tokens=meta.get("candidatesTokenCount"),
            total_tokens=meta.get("totalTokenCount"),
        )

    def extract_usage(self, response: dict) -> Optional[Usage]:
        meta = (response or {}).get("usageMetadata")
        return self._usage_from_metadata(meta) if meta else None

    def extract_stream_usage(self, sse_data: list[str]) -> Optional[Usage]:
        usage = None
        for payload in sse_data:
            chunk = self.safe_json(payload)
            # 流式分片可能是 dict 或 list[dict]
            items = chunk if isinstance(chunk, list) else [chunk]
            for item in items:
                if isinstance(item, dict) and item.get("usageMetadata"):
                    usage = item["usageMetadata"]
        return self._usage_from_metadata(usage) if usage else None

    def extract_error(self, status_code: int, response: Optional[dict]) -> Optional[str]:
        if status_code < 400 and not (response or {}).get("error"):
            return None
        err = (response or {}).get("error")
        if isinstance(err, dict):
            return err.get("message") or str(err)
        return f"HTTP {status_code}"

    def extract_cache_usage(self, response: Optional[dict]) -> Optional[dict]:
        """真实解析 usageMetadata.cachedContentTokenCount（上下文缓存命中时返回）。"""
        meta = (response or {}).get("usageMetadata") or {}
        n = meta.get("cachedContentTokenCount")
        if n is None:
            return None
        return {"cache_read_tokens": int(n), "cache_write_tokens": None}
