"""PricingRegistry 单元测试。"""
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from monitor.events import Usage
from monitor.pricing import PricingRegistry

pricing = PricingRegistry()

# 北京时间（UTC+8）的测试时间戳：8:00=低峰，10:00=高峰，15:00=高峰
_TZ = timezone(timedelta(hours=8))


def _ts(hour: int) -> float:
    return datetime(2026, 8, 20, hour, 0, 0, tzinfo=_TZ).timestamp()


def _ts_on(month: int, day: int, hour: int = 12) -> float:
    return datetime(2026, month, day, hour, 0, 0, tzinfo=_TZ).timestamp()


def test_exact_match():
    p = pricing.get_price("deepseek", "deepseek-chat")
    assert p and p.currency == "CNY" and p.input > 0


def test_prefix_match_fallback():
    # 带日期后缀的模型回退到母模型价格
    p = pricing.get_price("openai", "gpt-4o-2024-08-06")
    assert p and p.input == pricing.get_price("openai", "gpt-4o").input


def test_unknown_model_returns_none():
    assert pricing.get_price("openai", "no-such-model-xyz") is None
    assert pricing.get_price("unknown-provider", "gpt-4o") is None


def test_v4_flash_calibrated():
    """deepseek-v4-flash 已校准（2026-08-17 官方价），可命中。"""
    p = pricing.get_price("deepseek", "deepseek-v4-flash")
    assert p is not None and p.currency == "CNY"
    assert p.input == 1.5 and p.output == 4.5 and p.cache_hit == 0.05
    assert p.peak == {"input": 3.0, "output": 9.0, "cache_hit": 0.10}


def test_model_alias_case_insensitive():
    """DeepSeek-V4-Flash 大小写归一化后可命中（真实响应为小写）。"""
    assert pricing.get_price("deepseek", "DeepSeek-V4-Flash") is not None
    assert pricing.get_price("deepseek", "deepseek-v4-pro") is not None


# ---------- V4 Cache + Peak 成本 ----------

def test_v4_flash_cache_hit_offpeak():
    """低峰 + cache hit：hit_tokens × 0.05 + miss × 1.5 + out × 4.5。"""
    cost = pricing.compute_cost(
        "deepseek", "deepseek-v4-flash",
        Usage(input_tokens=3000, output_tokens=500, cache_read_tokens=1000),
        at=_ts(8))
    # 1000×0.05 + 2000×1.5 + 500×4.5 = 50+3000+2250 = 5300 → 0.0053
    assert abs(cost.amount - 0.0053) < 1e-9


def test_v4_flash_cache_miss_offpeak():
    """低峰 + 全 miss：3000×1.5 + 500×4.5。"""
    cost = pricing.compute_cost(
        "deepseek", "deepseek-v4-flash",
        Usage(input_tokens=3000, output_tokens=500, cache_read_tokens=0),
        at=_ts(8))
    # 4500+2250 = 6750 → 0.00675
    assert abs(cost.amount - 0.00675) < 1e-9


def test_v4_flash_mixed_cache():
    """hit=1000/miss=2000/out=500 全量拆分（低峰）。"""
    cost = pricing.compute_cost(
        "deepseek", "deepseek-v4-flash",
        Usage(input_tokens=3000, output_tokens=500, cache_read_tokens=1000),
        at=_ts(8))
    assert abs(cost.amount - 0.0053) < 1e-9   # 与 Test1 一致（同参数）


def test_v4_pro_cache():
    """V4-Pro 低峰：hit ×0.15 + miss ×4.5 + out ×13.5。"""
    cost = pricing.compute_cost(
        "deepseek", "deepseek-v4-pro",
        Usage(input_tokens=3000, output_tokens=500, cache_read_tokens=1000),
        at=_ts(8))
    # 150 + 9000 + 6750 = 15900 → 0.0159
    assert abs(cost.amount - 0.0159) < 1e-9


def test_v4_peak_vs_offpeak():
    """同一 usage 高峰 vs 低峰价格不同（flash）。"""
    u = Usage(input_tokens=3000, output_tokens=500, cache_read_tokens=1000)
    off = pricing.compute_cost("deepseek", "deepseek-v4-flash", u, at=_ts(8))
    peak = pricing.compute_cost("deepseek", "deepseek-v4-flash", u, at=_ts(10))
    assert peak.amount > off.amount
    # 高峰：1000×0.10 + 2000×3.0 + 500×9.0 = 100+6000+4500 = 10600 → 0.0106
    assert abs(peak.amount - 0.0106) < 1e-9


def test_v4_is_peak_boundaries():
    """峰谷边界：9:00 起、12:00 止、14:00 起、18:00 止（含开始不含结束）。"""
    assert pricing.is_peak("deepseek", at=_ts(8)) is False
    assert pricing.is_peak("deepseek", at=_ts(9)) is True
    assert pricing.is_peak("deepseek", at=_ts(12)) is False
    assert pricing.is_peak("deepseek", at=_ts(14)) is True
    assert pricing.is_peak("deepseek", at=_ts(18)) is False
    # 未配置峰谷的 Provider → False
    assert pricing.is_peak("openai", at=_ts(10)) is False


def test_no_cache_price_falls_back_to_input():
    """无 cache_hit 配置的模型（gpt-4o）不拆分 cache，沿用单输入价。"""
    cost = pricing.compute_cost(
        "openai", "gpt-4o",
        Usage(input_tokens=3000, output_tokens=500, cache_read_tokens=1000))
    # gpt-4o: input 2.5 / output 10.0，无 cache 分价 → 全量走单输入价
    assert abs(cost.amount - (3000 * 2.5 + 500 * 10.0) / 1e6) < 1e-9


def test_compute_cost():
    # 固定低峰窗口，避开 deepseek-chat 的峰谷配置影响
    cost = pricing.compute_cost("deepseek", "deepseek-chat",
                                Usage(input_tokens=1_000_000, output_tokens=500_000),
                                at=_ts(8))
    price = pricing.get_price("deepseek", "deepseek-chat")
    assert abs(cost.amount - (price.input + price.output * 0.5)) < 1e-6
    assert cost.currency == "CNY"


def test_compute_cost_missing_usage_no_fabrication():
    assert pricing.compute_cost("deepseek", "deepseek-chat", None) is None
    assert pricing.compute_cost("deepseek", "deepseek-chat", Usage()) is None
    assert pricing.compute_cost("deepseek", "unknown-model",
                                Usage(input_tokens=1, output_tokens=1)) is None


# ---------- 价格版本选择（effective_date） ----------

def test_v4_flash_version_before_0817():
    """08-17 前事件选旧档（命中 0.02 / 未命中 1.0 / 输出 2.0，无峰谷）。"""
    p = pricing.get_price("deepseek", "deepseek-v4-flash", at=_ts_on(8, 10))
    assert p is not None
    assert p.input == 1.0 and p.output == 2.0 and p.cache_hit == 0.02
    assert p.peak is None


def test_v4_flash_version_after_0817():
    """08-17 起事件选新档（峰值 0.05/1.5/4.5 + 峰谷）。"""
    p = pricing.get_price("deepseek", "deepseek-v4-flash", at=_ts_on(8, 20))
    assert p is not None
    assert p.input == 1.5 and p.output == 4.5 and p.cache_hit == 0.05
    assert p.peak == {"input": 3.0, "output": 9.0, "cache_hit": 0.10}


def test_v4_flash_version_no_at_uses_latest():
    """不传 at 取最新版（08-17 档），保持旧调用兼容。"""
    p = pricing.get_price("deepseek", "deepseek-v4-flash")
    assert p is not None and p.input == 1.5 and p.cache_hit == 0.05


def test_v4_flash_compute_cost_respects_version():
    """同一 usage，旧档（低峰）成本应低于新档（低峰）。"""
    u = Usage(input_tokens=3000, output_tokens=500, cache_read_tokens=1000)
    old = pricing.compute_cost("deepseek", "deepseek-v4-flash", u, at=_ts_on(8, 10, 8))
    new = pricing.compute_cost("deepseek", "deepseek-v4-flash", u, at=_ts_on(8, 20, 8))
    # 旧档：1000×0.02 + 2000×1.0 + 500×2.0 = 20+2000+1000 = 3020 → 0.00302
    assert abs(old.amount - 0.00302) < 1e-9
    # 新档：1000×0.05 + 2000×1.5 + 500×4.5 = 5300 → 0.0053
    assert abs(new.amount - 0.0053) < 1e-9
