"""ProviderRegistry：Gateway 不做任何 Provider 硬编码判断，全部走注册表。

新增 Provider 只需三步：
1. 新增 Adapter（OpenAI 兼容的直接复用 OpenAICompatibleAdapter）
2. 在 PROVIDER_DEFS 注册一行
3. 在 pricing_data.yaml 添加定价
"""
from __future__ import annotations

from typing import Optional

from .providers import (GeminiAdapter, GenericAdapter, OpenAICompatibleAdapter,
                       ProviderAdapter)

PROVIDER_DEFS: dict[str, dict] = {
    "openai": {
        "kind": "openai_compat",
        "default_base_url": "https://api.openai.com",
    },
    "deepseek": {
        "kind": "openai_compat",
        "default_base_url": "https://api.deepseek.com",
    },
    "kimi": {
        "kind": "openai_compat",
        "default_base_url": "https://api.moonshot.cn",
    },
    "minimax": {
        "kind": "openai_compat",
        "default_base_url": "https://api.minimaxi.com",
    },
    "gemini": {
        "kind": "gemini",
        "default_base_url": "https://generativelanguage.googleapis.com",
    },
    # ---- 以下预注册，用户配置 base_url + key 即可用 ----
    "openrouter": {
        "kind": "openai_compat",
        "default_base_url": "https://openrouter.ai/api",
    },
    "groq": {
        "kind": "openai_compat",
        "default_base_url": "https://api.groq.com/openai",
    },
    "zhipu": {
        "kind": "openai_compat",
        "default_base_url": "https://open.bigmodel.cn/api/paas/v4",
        "api_prefix": "",   # base_url 已含完整前缀
    },
    "qwen": {
        "kind": "openai_compat",
        "default_base_url": "https://dashscope.aliyuncs.com/compatible-mode",
    },
    "doubao": {
        "kind": "openai_compat",
        "default_base_url": "https://ark.cn-beijing.volces.com/api/v3",
    },
}


class ProviderRegistry:
    def __init__(self):
        self._adapters: dict[str, ProviderAdapter] = {}
        self._defaults: dict[str, str] = {}
        for name, spec in PROVIDER_DEFS.items():
            self.register(name, spec)
        self._generic = GenericAdapter()  # 兜底解析器（任意 OpenAI-compat / Gemini 形态）

    def generic(self) -> ProviderAdapter:
        """未知 Provider 的兜底解析器；不进入注册表（无固定 base_url）。"""
        return self._generic

    def register(self, name: str, spec: dict) -> None:
        kind = spec["kind"]
        if kind == "openai_compat":
            adapter = OpenAICompatibleAdapter(
                name=name,
                default_base_url=spec["default_base_url"],
                api_prefix=spec.get("api_prefix", "/v1"),
            )
        elif kind == "gemini":
            adapter = GeminiAdapter()
        elif kind == "generic":
            adapter = GenericAdapter()
        else:
            raise ValueError(f"unknown adapter kind: {kind}")
        self._adapters[name] = adapter
        self._defaults[name] = spec["default_base_url"]

    def get(self, name: str) -> Optional[ProviderAdapter]:
        return self._adapters.get(name)

    def default_base_url(self, name: str) -> str:
        return self._defaults.get(name, "")

    def names(self) -> list[str]:
        return list(self._adapters)
