"""PricingRegistry：成本计算的唯一入口。

禁止在业务代码里出现 `if model == "xxx"` 的价格硬编码；
所有价格查询必须经过本模块。支持精确匹配与前缀匹配（如 gpt-4o-2024-08-06 → gpt-4o）。

Peak / Off-Peak：
  - 仅 Provider 在 pricing_data.yaml 配置了 peak.hours 时启用（当前 DeepSeek V4）
  - DeepSeek 官方规则按北京时间（UTC+8，无夏令时）9:00-12:00、14:00-18:00 为高峰
  - 计算统一以 UTC 时间戳 + 固定 UTC+8 偏移判定，避免本地/UTC 混用

Cache 分价：
  - 模型配置 cache_hit 时启用（cache_read_tokens 命中价 + 剩余输入走 miss 价）
  - 未配置 cache_hit 时沿用单输入价（兼容旧 Provider）
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import yaml

from .events import Usage

DEFAULT_DATA = Path(__file__).parent / "pricing_data.yaml"

# DeepSeek 峰谷按北京时间（UTC+8，无夏令时）
_CN_TZ = timezone(timedelta(hours=8))


@dataclass
class ModelPrice:
    input: float          # 每 100 万 token（cache miss / 未配置 cache_hit 时的输入价）
    output: float
    currency: str
    effective_date: str = ""
    cache_hit: Optional[float] = None     # 每 100 万 token；None=不支持 cache 分价
    peak: Optional[dict] = None           # {"input":.., "output":.., "cache_hit":..} 高峰价


@dataclass
class Cost:
    amount: float
    currency: str


class PricingRegistry:
    def __init__(self, data_path: str | Path = DEFAULT_DATA):
        self._prices: dict[str, dict[str, list[ModelPrice]]] = {}
        self._peaks: dict[str, list[tuple[int, int]]] = {}
        self.reload(data_path)

    def reload(self, data_path: str | Path = DEFAULT_DATA) -> None:
        data = yaml.safe_load(Path(data_path).read_text(encoding="utf-8")) or {}
        prices: dict[str, dict[str, list[ModelPrice]]] = {}
        peaks: dict[str, list[tuple[int, int]]] = {}
        for provider, spec in data.items():
            spec = spec or {}
            currency = spec.get("currency", "USD")
            # Provider 级峰谷配置：peak.hours: [[9,12],[14,18]]
            hours = (spec.get("peak") or {}).get("hours") or []
            peaks[provider] = [(int(s), int(e)) for s, e in hours]
            models: dict[str, list[ModelPrice]] = {}
            for model, p in ((spec.get("models") or {})).items():
                if p is None:
                    models[model] = []
                    continue
                # 支持单版本（dict）或多版本（list，按 effective_date 选价）
                entries = p if isinstance(p, list) else [p]
                versions: list[ModelPrice] = []
                for e in entries:
                    e = e or {}
                    versions.append(ModelPrice(
                        input=float(e.get("input", 0.0)),
                        output=float(e.get("output", 0.0)),
                        currency=currency,
                        effective_date=str(e.get("effective_date", "")),
                        cache_hit=(float(e["cache_hit"])
                                   if e.get("cache_hit") is not None else None),
                        peak=e.get("peak") if isinstance(e.get("peak"), dict) else None,
                    ))
                models[model] = versions
            prices[provider] = models
        self._prices = prices
        self._peaks = peaks

    @staticmethod
    def _select_version(versions: list[ModelPrice], at: Optional[float]):
        """按事件时间戳 at 选生效版本：effective_date <= at 日期的最新版；
        at=None 取最新版；at 早于所有版本时回退最早版（不伪造 None）。"""
        if not versions:
            return None
        sorted_v = sorted(versions, key=lambda v: v.effective_date or "0000-00-00")
        if at is None:
            return sorted_v[-1]
        at_date = datetime.fromtimestamp(at, tz=_CN_TZ).strftime("%Y-%m-%d")
        chosen = None
        for v in sorted_v:
            if (v.effective_date or "0000-00-00") <= at_date:
                chosen = v
            else:
                break
        return chosen or sorted_v[0]

    def get_price(self, provider: str, model: Optional[str],
                  at: Optional[float] = None) -> Optional[ModelPrice]:
        if not model:
            return None
        # model 大小写归一（真实响应 deepseek-v4-flash 小写；支持 DeepSeek-V4-Flash 等）
        model = model.strip().lower()
        models = self._prices.get(provider, {})
        versions: Optional[list[ModelPrice]] = None
        if model in models:
            versions = models[model]
        else:
            # 前缀匹配：带日期/版本后缀的模型名回退到母模型价格
            candidates = [m for m in models if model.startswith(m)]
            if candidates:
                versions = models[max(candidates, key=len)]
        if not versions:
            return None
        return self._select_version(versions, at)

    def is_peak(self, provider: str, at: Optional[float] = None) -> bool:
        """判断 at（UTC epoch）是否处于该 Provider 的高峰时段；未配置则 False。"""
        hours = self._peaks.get(provider) or []
        if not hours:
            return False
        dt = datetime.fromtimestamp(at if at is not None else __import__("time").time(),
                                    tz=_CN_TZ)
        return any(start <= dt.hour < end for start, end in hours)

    def compute_cost(self, provider: str, model: Optional[str],
                     usage: Optional[Usage],
                     at: Optional[float] = None) -> Optional[Cost]:
        """usage / 价格缺失时返回 None，不估算、不伪造。

        at：可注入 UTC 时间戳（测试峰谷用）；默认当前时间。
        成本 = cache_hit_tokens×cache_hit_price + cache_miss_tokens×input_price
             + output_tokens×output_price（cache_hit 未配置时 input_tokens×input_price）
        """
        if not usage:
            return None
        if usage.input_tokens is None and usage.output_tokens is None:
            return None
        price = self.get_price(provider, model, at)
        if not price:
            return None

        peak_price = price.peak if (self.is_peak(provider, at) and price.peak) else None
        inp_price = float(peak_price["input"]) if peak_price else price.input
        out_price = float(peak_price["output"]) if peak_price else price.output
        hit_price = (float(peak_price["cache_hit"])
                     if peak_price and peak_price.get("cache_hit") is not None
                     else price.cache_hit)

        in_tok = usage.input_tokens or 0
        hit_tok = usage.cache_read_tokens or 0
        out_tok = usage.output_tokens or 0

        if hit_price is not None and hit_tok > 0:
            miss_tok = max(0, in_tok - hit_tok)   # prompt_tokens = hit + miss
            input_cost = hit_tok * hit_price + miss_tok * inp_price
        else:
            input_cost = in_tok * inp_price
        amount = (input_cost + out_tok * out_price) / 1_000_000
        return Cost(amount=round(amount, 6), currency=price.currency)
