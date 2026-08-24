"""ProviderAdapter 统一接口。

Monitor Core 只面向这个接口编程；Provider 协议差异全部在 Adapter 内解决。
"""
from __future__ import annotations

import json
from abc import ABC, abstractmethod
from typing import Any, Optional

from ..config import ProviderConfig
from ..events import Usage


class ProviderAdapter(ABC):
    name: str = ""

    # 能力声明：无真实 API 数据来源时保持 False，前端据此显示 N/A
    balance_supported: bool = False
    usage_supported: bool = False
    cache_supported: bool = False

    # ---------- 请求侧 ----------

    @abstractmethod
    def upstream_url(self, cfg: ProviderConfig, path: str) -> str:
        """把 gateway 收到的相对路径映射为 Provider 真实 URL。"""

    @abstractmethod
    def upstream_headers(self, cfg: ProviderConfig, client_headers: dict) -> dict:
        """构造发往 Provider 的请求头（注入鉴权，剔除客户端鉴权）。"""

    def upstream_body(self, body: Optional[dict]) -> Optional[dict]:
        """转发前可改写 body（如注入 stream_options 以获取流式 usage）。"""
        return body

    @staticmethod
    def is_stream(body: Optional[dict], path: str) -> bool:
        return bool(body and body.get("stream"))

    # ---------- 响应侧 ----------

    @abstractmethod
    def extract_model(self, path: str, body: Optional[dict],
                      response: Optional[dict]) -> Optional[str]:
        """从路径/请求/响应中确定模型名。"""

    @abstractmethod
    def extract_usage(self, response: dict) -> Optional[Usage]:
        """非流式响应的 usage 提取。"""

    @abstractmethod
    def extract_stream_usage(self, sse_data: list[str]) -> Optional[Usage]:
        """流式响应的 usage 提取。sse_data 为所有 `data:` 行的原始文本。"""

    @abstractmethod
    def extract_error(self, status_code: int, response: Optional[dict]) -> Optional[str]:
        """从错误响应中提取可读错误信息。"""

    def extract_cache_usage(self, response: Optional[dict]) -> Optional[dict]:
        """Cache 用量提取。默认无数据返回 None；
        支持时返回 {"cache_read_tokens": int|None, "cache_write_tokens": int|None}。
        绝不估算：Provider 未返回真实 cache 指标时保持 None。"""
        return None

    def balance(self, cfg) -> Optional[dict]:
        """Provider 余额。无官方 API 时返回 None（前端显示 N/A）。
        支持时返回 {"balance": float, "currency": str}。"""
        return None

    # ---------- 工具 ----------

    @staticmethod
    def parse_sse_lines(raw: bytes | str) -> list[str]:
        """拆分 SSE 字节流，返回所有 data: 载荷。"""
        text = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else raw
        out = []
        for line in text.splitlines():
            line = line.strip()
            if line.startswith("data:"):
                payload = line[5:].strip()
                if payload and payload != "[DONE]":
                    out.append(payload)
        return out

    @staticmethod
    def safe_json(text: str) -> Optional[Any]:
        try:
            return json.loads(text)
        except (ValueError, TypeError):
            return None
